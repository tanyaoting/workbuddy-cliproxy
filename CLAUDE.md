# CLAUDE.md

给下一个接手这个仓库的 agent 看的现场笔记。目标：读完你就拥有和上一轮会话相同的理解，
不用重新踩一遍坑。

---

## 1. 这是什么

一个 CLIProxyAPI（CPA）**原生插件**，把腾讯 CodeBuddy 包装成 CPA 的一个 provider。

- **仓库**：`github.com/lovingfish/workbuddy-cliproxy`
- **作者身份**：对 [Sliverkiss/cpa-plugin](https://github.com/Sliverkiss/cpa-plugin) 公开的
  `workbuddy.so` 做 clean-room 逆向重写。原始设计归属 Sliverkiss。
- **代码规模**：单文件 `main.go`，约 1370 行，无子包。
- **依赖**：只有一个 —— `github.com/router-for-me/CLIProxyAPI/v8`（用它的 `sdk/pluginabi`
  和 `sdk/pluginapi`）。

数据流：

```
客户端(Claude Code/Cursor/SDK)
  → CPA :8317
  → 插件 executor.execute / execute_stream
  → https://copilot.tencent.com/v2/chat/completions
```

---

## 2. 构建与部署

### 核心命令

```bash
./build-and-deploy.sh          # 一键：编译 → 校验 → 部署 → 重启 → 验证
```

### 编译本质

```bash
go build -buildmode=c-shared -o dist/workbuddy.so .
```

必须 `CGO_ENABLED=1`、`GOOS=linux`、`GOARCH` 与目标容器一致。**macOS 本机编不了**
（缺 Linux C 工具链，会在 `runtime/cgo` 报 `clearenv` / `setresgid` 隐式声明错误）——
所以脚本在 `golang:1.26-bookworm` 容器里编。

### 踩过的坑（都已在脚本里修好，改脚本时别改回去）

| 坑 | 说明 |
|---|---|
| 输出路径必须用容器内相对路径 | 写宿主机绝对路径不报错，但文件落进容器临时层，随 `--rm` 消失 |
| `docker cp` 不建父目录 | 目标 `plugins/` 不存在会直接失败，脚本先 `mkdir -p` |
| glibc 基线 | `bookworm` 要求 glibc ≥ 2.34；更老的系统（Ubuntu 20.04, glibc 2.31）要用 `golang:1.21-bullseye` |
| **API key 在 v8 挪了位置** | `api-keys` 从顶层移进 `access:` 段，锚定行首的 grep 会匹配到另一个空的顶层节，导致验证被静默跳过。详见第 10 节 |
| 编译产物会被覆盖 | 脚本自动备份到 `dist/.backup/`（保留 5 份，`KEEP_BACKUPS` 可调） |

### 容器布局不统一 —— 脚本会自动探测

不同 CPA 镜像的目录不一样，脚本按序找「同时有 `config.yaml` 和 `plugins/`」的目录：

```
/CLIProxyAPI  /app  /data  /opt/cliproxyapi
```

**当前实测环境**：容器 `cpa`，镜像 `eceasy/cli-proxy-api:latest` = **CLIProxyAPI v8.0.16**，
工作目录 `/CLIProxyAPI`。

该镜像把数据全部 bind mount 到宿主机：

```
/Users/apple/.cli-proxy-api/config.yaml   → /CLIProxyAPI/config.yaml
/Users/apple/.cli-proxy-api/plugins/      → /CLIProxyAPI/plugins/
/Users/apple/.cli-proxy-api/auths/        → /CLIProxyAPI/auths/
/Users/apple/.cli-proxy-api/static/       → /CLIProxyAPI/static/
```

**含义**：插件文件实际活在宿主机 `~/.cli-proxy-api/plugins/workbuddy.so`，换容器不丢。

### Go 缓存

挂在 Docker volume `workbuddy-gopath-arm64` / `workbuddy-gocache-arm64`（约 104MB）。
冷编译 15s，热编译 **1~2s**。清掉重来：

```bash
docker volume rm workbuddy-gopath-arm64 workbuddy-gocache-arm64
```

---

## 3. 插件 ABI —— 怎么和 CPA 对接

`main.go` 是 cgo 包（`package main` + 空 `main()`），文件头 preamble 手写了 C 结构体定义，
导出 **4 个符号**（脚本会校验数量，少于 4 就是 ABI 不完整）：

| 符号 | 作用 |
|---|---|
| `cliproxy_plugin_init` | CPA 加载时注入 host 函数指针表，插件保存 `hostAPI` |
| `cliproxyPluginCall` | 所有 RPC 的入口，按 `method` 字符串分发（`handleMethod`） |
| `cliproxyPluginFree` | 释放 host 返回的 buffer |
| `cliproxyPluginShutdown` | 空实现 |

**host API 是函数指针表**，插件通过它反向调用 CPA（`hostCall`），用于异步流式
`host.stream.emit` / `host.stream.close`。

### ABI 版本兼容性（重要）

`pluginabi.ABIVersion` 在 **v7.2.30 和 v8.0.16 都是 `1`**。所以 v7 编译的插件能在 v8
容器里加载，反之亦然。**升级 CPA 大版本时先查这个常量**，值没变就大概率兼容。

### RPC 方法分发

```
plugin.register / plugin.reconfigure  → wbRegistration()  声明 capability
model.static                          → wbModelsCached()  不带凭据，非阻塞
model.for_auth                        → handleModelsForAuth()  带凭据，可联网刷新
auth.identifier / auth.parse          → 凭据解析
auth.login.start / auth.login.poll    → 扫码登录流程
auth.refresh                          → token 刷新
executor.identifier                   → "workbuddy"
executor.execute                      → 非流式（内部转流式再聚合）
executor.execute_stream               → 流式，支持真异步
```

---

## 4. 模型清单 —— 曾经的深坑

**这是这个仓库最容易做错的地方，单独一节。**

### 权威来源：`GET /v3/config`

```bash
curl https://copilot.tencent.com/v3/config \
  -H "Authorization: Bearer <accessToken>" \
  -H "X-User-Id: <uid>"
```

返回 `{code, msg, data:{models:[...], agents:[...], productFeatures, ...}}`。

**为什么它是权威的**：

1. 官方客户端自己就读这个接口 —— 桌面端 `product.json` 里的
   `ACC_PRODUCT_CONFIG_V3` / `ACC_PRODUCT_CONFIG_V2` 环境变量注入的就是同一份数据
2. 按凭据返回，反映当前账号真实可见的模型
3. **服务端上新模型后不需要改代码或重新编译**，但**必须重启 CPA 进程**才看得到 ——
   见下面「缓存冻结」一节

三个域名都可用且返回相同内容：`copilot.tencent.com` / `www.workbuddy.cn` / `www.codebuddy.cn`。

### 实现（`main.go` 中段）

- `endpointConfig` — 端点常量
- `remoteModel` / `remoteConfig` — 只解码转发给 CPA 的字段
- `fetchRemoteModels(sa)` — 带鉴权拉取
- `isChatModel(m)` — 过滤：`maxInputTokens <= 0` 即非对话模型（图片/视频生成后端）
- `catalogFromRemote` — 转换 + 去重
- `handleModelsForAuth` — 30 分钟 TTL 刷新，失败降级
- `wbModelsCached` — 非阻塞读缓存
- `seedModels()` — 内置兜底表（仅在完全拉不到时用）

**降级链**：`/v3/config` 失败 → 上次成功结果 → `seedModels()`。

### ⚠️ 缓存冻结 —— 上新模型后必须重启 CPA

`handleModelsForAuth` 里的 30 分钟 TTL **只在被调用时才起作用**，而 CPA 只在
**插件加载时调一次 `model.for_auth`**。之后：

- `/v1/models` 走 `model.static`（`main.go:241`）—— 只读内存缓存，**永不联网**
- `model.for_auth` 不再被调用 → TTL 永远不检查 → 缓存冻在进程启动那一刻的版本

**实测证据（2026-10-13）**：容器 10-06 启动，缓存冻在 10-06。
`step-5-preview` 是之后上线的，插件列表里一直没有（60→61 个模型都对不上），
但直接打上游接口**能正常返回**（`content: "OK"`, `finish_reason: stop`）。
`docker restart cpa` 后立刻出现，模型总数 61 → 62。

**排查手法**：插件列表缺模型，而直接 curl 上游 `/v3/config` 和
`/v2/chat/completions` 都正常 → 就是缓存冻结，重启即可，**不用重新编译**。

**没法绕开重启**（都试过，全堵死）：

| 尝试 | 结果 |
|---|---|
| `/v8/management/credentials` 触发刷新 | 401，`secret-key` 是 bcrypt hash，明文已丢失 |
| 公开刷新路由（`/v1/models/refresh` 等） | 全 404 |
| 只读日志确认调用 | 插件无日志输出，查不到 |

### 探测模型可用性的方法

上游**没有**模型列表 API（我扫过 30+ 个候选路径，全 404）。唯一的验证手段是
**发一个真实请求看返回码**：

```bash
curl -s https://copilot.tencent.com/v2/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H "X-User-Id: $UID" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  -d '{"model":"<id>","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

| 返回 | 含义 |
|---|---|
| `chat.completion.chunk` | ✅ 可用 |
| `{"code":11102,...}` | 模型不在服务（过期/未开通） |
| `{"code":11103,...}` | 后端不支持流式（图片/视频类） |
| `{"code":11101,...}` | 拒绝非流式请求 → **所以插件强制 `stream:true`** |

### ⚠️ 已知陷阱：`product.json` 不可信

桌面端 `WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/product.json`（377KB）
看起来像权威模型表，**但实测已过期且含假信息**，不要再用它当来源：

| 问题 | 证据 |
|---|---|
| 声明与行为矛盾 | 多个模型标 `onlyReasoning: true`（"只输出思考"），实测 `reasoning_tokens: 0`，直接输出答案 |
| 含大量不可用项 | 49 个 id 里约一半返回 11102/11103 |
| 缺少服务端独有项 | `deepseek-v4.1-flash`、`hy4-preview`、`glm-5.3`、`space-bunny` 都不在表里却能用 |
| 混入第三方配置 | 同目录 `codebuddy-headless.js` 里有一份 **opencode** 的模型注册表（`provider: "opencode-go"`、`baseUrl: opencode.ai`），与腾讯无关 |

同目录还有 4 个变体（`product.ioa.json` 93 个模型 / `.internal` 46 / `.cloudhosted` 22 /
`.selfhosted` 1），**都别用**。

### `auto` 是分档路由，不是模型

`AUTO_TIER_MODELS`（在 `codebuddy-headless.js` 里）定义三个档位，实测路由结果：

| 请求 id | 实际路由到 |
|---|---|
| `fast-model`（快速） | `deepseek-v4.1-flash` |
| `balanced-model`（均衡） | `hy4-preview` |
| `deep-model`（极致） | `kimi-k3` |
| `auto` | `hy4-preview`（= 均衡档） |

**这解释了 `deepseek-v4.1-flash` 的来源** —— 它是「快速档」背后的真实模型，不是给用户
直接点的模型，所以不在客户端 UI 配置里。

### 已排除的模型类别：`codewise-*`

`codewise-completions` / `-rewrite` / `-jump` / `-nes-a4-027-aide` **实测能通过 chat 接口
返回正常 chunk，但已从清单剔除**。原因（依据来自 product.json 的官方描述字段）：

- 官方描述是 IDE 任务模型：「专门用于代码重构和重写任务」「代码导航」
- `maxOutputTokens: 256`，正常对话根本不够
- 需要专用 prompt 模板并绑定编辑器状态（见 `completion.jumpToHere` 的
  `variables: [activeEditor, workspaceFileDiffs, diagnostics]`）

**判定标准：接口能通 ≠ 是对话模型。** 加新模型时留意这类。

---

## 5. 认证

扫码登录流程（`handleStartLogin` / `handlePollLogin`）。要点：

- CodeBuddy 把浏览器登录绑在 `auth/state` 下发的 state 上，**必须复用同一个 cookie jar**
  跨 state 请求和后续轮询 → 所以有 `newLoginClient()` 隔离每个登录流程
- 凭据存成 `workbuddy.json`（含 access/refresh token，**已在 .gitignore 里，别提交**）
- 请求头用 `X-No-*` 约定表示空字段：

```
Authorization: Bearer <accessToken>   或   X-No-Authorization: 1
X-User-Id: <uid>                      或   X-No-User-Id: 1
X-Enterprise-Id: <id>                 或   X-No-Enterprise-Id: 1
X-Refresh-Token: <token>
```

- 固定头：`User-Agent: CLI/2.63.2 CodeBuddy/2.63.2`、`Origin`/`Referer: https://www.codebuddy.cn`

---

## 6. 流式与内容改写

### 真流式（异步）

`handleExecStream` 拿到 host 的 `stream_id` 后立刻返回，由 goroutine
`pumpUpstreamStream` 边读上游边 `streamEmit` 推给 CPA。客户端逐字收到。
没有 `stream_id` 时降级为同步收集（`collectUpstreamStream`）。

### SSE 帧化判断

`clientNeedsSSEFrame(metadata)`：CPA 的原生 chat-completions 透传会自己加 `data: ` 前缀，
但 claude/gemini/codex 等跨格式翻译器**只认已经带 `data: ` 的 payload**。
所以按请求路径决定要不要自己帧化。

### 内容审核绕过（cat-and-mouse）

腾讯 CodeBuddy 把若干客户端固定的 system 模板**逐字**加进了黑名单，命中即回"敏感内容"。
流式请求下表现为 **HTTP 200 + SSE 里的 error 事件**（CPA 访问日志里只有 200，
`upstream 400: {"code":11128,..."Illegal API invocation from an unapproved channel"}` 只出现在 SSE 里），
排查时不能只看访问日志的状态码。

当前已知条目：

- `You are Claude Code, Anthropic's official CLI for Claude.` → `CLI`→`CLI tool`
- `Main branch (you will usually use this for PRs)` → `Main branch`→`Default branch`
- dsh（DeepSeek Harness）的 system 模板 —— **不止一条，且常是跨句长子串**：
  - 身份句 `You are an AI agent powered by DeepSeek Harness`
  - Web GUI 段 `You are interacting with the user through the DeepSeek Harness Web GUI
    whose URL the runtime context names. When the user refers to "this page", ...`
    —— 两句**合起来**才命中，单发任一句都能过

**所以别按句子一条条补**：dsh 这条前后补了两版，第一版只改身份句，CLI 通但 Web GUI
仍被拦（GUI 的 system 提示词多出上面那段）。最终做法是**整体替换产品名**
`DeepSeek Harness` → `DeepSeek Agent`，所有已知指纹都含这个词组，一次跳出，也不用
跟着腾讯加句子。

**匹配的是子串、不含句末标点**：`...DeepSeek Harness.`、`...Harness, a coding agent.`、
`...Harness agent.` 全部命中，只有在 `DeepSeek Harness` 之前插入单词才跳出。要改就改
**被命中的那段子串本身**，在句尾追加文字绕不过去。

> 腾讯哪天加一条**不含** `DeepSeek Harness` 的 dsh 指纹，就得回到按句子处理。

### hy3 系列强制最大思考

`forceMaxThinking`：对 `hy3*` 开头的模型强制 `reasoning_effort=high`，覆盖客户端设置。
CodeBuddy **只认 `high`** 才真开深度思考（`medium`/`max`/`xhigh` 都忽略）。
思考内容走 SSE 的 `delta.reasoning_content`。

### 非流式请求强制转流式

`forceStreamBody`：上游对非流式请求返回 `11101`，所以一律改成 `stream:true`
再内聚合成单个 `chat.completion` 对象（`aggregateCompletion`）。

---

## 7. 模型元数据说明（哪些可信）

CPA 的 `/v1/models` 端点**只返回 `id` / `object` / `owned_by`**，不透传任何元数据，
所以从外部看不到 `context_length` 之类是否生效。

| 字段 | 可信度 |
|---|---|
| `maxInputTokens` / `maxOutputTokens` | ✅ 来自 `/v3/config`，但**未逐项实测** |
| `supportsToolCall` / `supportsImages` | ⚠️ 只有服务端声明，**没验证过**。标错会导致 agent 类客户端直接失败 |
| `onlyReasoning` / `supportsReasoning` / `reasoning.effort` | ❌ **已被证伪**，实测 `reasoning_tokens: 0` |

**如果要用工具调用，先实测再信。**

另：`/v3/config` 返回的清单里**混有当前账号不可用的模型**（如 `glm-4.6`/`glm-4.7` 实测
11102）。当前实现（方案 C）不做可用性探测，所以它们会出现在列表里，调用时才报错。
这是刻意的取舍 —— 避免启动时打 30 个探测请求。

### 下游：CCR 的上下文窗口识别

模型列表交给 **claude-code-router（CCR）** 后，CCR 还要自己判断每个模型支持多大的
上下文窗口。**插件填的元数据 CCR 不读**（CPA 的 `/v1/models` 只给 `id`），两边独立。

**CCR 的判定链路**（反编译 `dist/main/router-plugin.js`）：

```js
function Q(e){
  for(r of Fl(e)) if(o=byKey.get(r))            return o   // 一级：id + aliases
  for(r of Fl(e)) if(o=byModelKey.get(Tr(r)))   return o   // 二级：裸模型名
  return El(e)                                             // 三级：硬编码
}
```

- **一级 `byKey`** — 记录的 `id` 和 `aliases` 都进这个索引
- **二级 `byModelKey`** — 裸名。同名记录 >1 条时**冲突被抹成 `undefined`**，
  `Q()` 里 `if(i)` 判假跳过，等于永久放弃
- **三级 `El()`** — **只对 `gpt-5.6*` 返回硬编码值（1050000/128000），其余一律 `undefined`**

**⚠️ 未被识别的模型不是「默认 200k」，是「无信息」。** 下游读到 `undefined` →
`!!undefined` → `false`，具体数值不保留。

**为什么 `cpa/*` 全部落空**：CCR 的 provider 名是 `cpa`，查询 key 形如
`cpa/step-5-preview`，但内置表里没有 `cpa` 这个 provider，一级必然落空；二级又因
裸名冲突（`glm-5.3` 有 18 条记录、`kimi-k3` 17 条）也命中不了。

**修法：改 `dist/models.json`。** 另有 `modelMetadata` 配置口子（provider 上加字段，
见 `gp()` / `Dg()`），**已实测 CCR 不读，别走那条路**。

**工具**：`~/.claude-code-router/patch-ccr-models.py`（不在本仓库，但为这个项目写的）

| 表 | 用途 |
|---|---|
| `WANT` | 表里**已有同名宿主**时，给宿主追加 `cpa/xxx` alias。⚠️ 会连宿主数值一起继承 |
| `NEW` | 表里**没有宿主**时新建独立记录，数值可直接填 `/v3/config` 真值。id 本身即进 `byKey` |

**踩过的坑**：

| 坑 | 说明 |
|---|---|
| 宿主数值不可信 | `kimi-k2.6` 上游 out=32000、宿主 `moonshotai/kimi-k2.6` 是 262144（**8 倍**）；`minimax-m2.5` 上游 ctx=200000、宿主 1000000（**5 倍**） |
| 很多模型没宿主 | 31 个 workbuddy 模型里 11 个搜不到同名记录，只能走 `NEW` |
| 表属 npm 包 | `package.json` 的 `files` 含 `dist`，**升级会被覆盖**，升级后重跑脚本 |
| 改完必须重启 CCR | `ccr stop && ccr start`，没有热重载 |

**当前覆盖状态（2026-10-13）**：62 个模型（31 workbuddy + 31 cursor）里只修了 2 个 ——
`cpa/deepseek-v4.1-flash`（alias，借宿主值）和 `cpa/step-5-preview`（新建，上游真值
1000000/64000）。**其余 60 个仍未识别。**

**上游真值表**（`/v3/config` 的 `maxInputTokens` / `maxOutputTokens`，未识别的模型照这个填）：

| 模型 | ctx | out | | 模型 | ctx | out |
|---|---|---|---|---|---|---|
| `deepseek-v4-pro` | 1000000 | 128000 | | `kimi-k2.6` | 256000 | 32000 |
| `deepseek-v4-flash` | 1000000 | 50000 | | `kimi-k2.7` | 256000 | 32000 |
| `deepseek-v4.1-flash` | 1000000 | 128000 | | `kimi-k2-thinking` | 164000 | 32000 |
| `deepseek-v3-2-volc` | 96000 | 32000 | | `kimi-k2.5` | 164000 | 32000 |
| `glm-5.3` | 1000000 | 64000 | | `minimax-m3` | 512000 | 64000 |
| `glm-5.3-flash` | 1000000 | 131072 | | `minimax-m2.5` / `m2.7` | 200000 | 48000 |
| `glm-5.2` | 1000000 | 64000 | | `hunyuan-chat` | 200000 | 8192 |
| `glm-5.1` / `5.0` / `5.0-turbo` | 200000 | 48000 | | `hy3` / `hy3-x` | 192000 | 64000 |
| `glm-5v-turbo` | 200000 | 64000 | | `hy4-preview` / `-x` | 960000 | 64000 |
| `glm-4.7` | 200000 | 48000 | | `space-bunny` | 1000000 | 128000 |
| `glm-4.6` | 168000 | 32000 | | `step-5-preview` | 1000000 | 64000 |
| `glm-4.6v` | 128000 | 32000 | | `kimi-k2.8-preview` | 1000000 | 64000 |
| `default` | 200000 | 24000 | | `kimi-k3-1` | 1000000 | 32000 |

`cursor/*` 那 31 个上游 `/v3/config` 里没有，**拿不到权威值**，要么跳过要么自己实测。

---

## 8. Git 与协作约定

- **不主动提交**。改动留在工作区，等用户明确说「提交」。
- 提交前先 `git status` + 审查 diff，确认无调试代码与无关文件。
- message 用中文 Conventional Commits（`feat`/`fix`/`refactor`/`chore`/`docs`…）。
- 本项目源码文件**不加注释落款**（落款规范只适用于配置与文档文件）。
- 提交信息结尾加：

```
Co-Authored-By: Claude Code <noreply@anthropic.com>
```

---

## 9. 已知问题 / 待办

1. **`forceMaxThinking` 只覆盖 hy3** —— `/v3/config` 里 deepseek-v4 等也标了
   `reasoning.effort: "high"`，插件没管，客户端传 `low` 会被接受。未确认是否有意为之。
2. **`supportsToolCall` 全部未验证** —— 见第 7 节。要用 agent 类客户端前先实测。
3. **`auto` / `default` 是路由别名** —— 元数据填的是别名自身的值，不代表实际后端。
4. **`/v3/config` 含不可用模型** —— 方案 C 不做可用性探测，`glm-4.6`/`glm-4.7` 之类会
   出现在列表里，调用时才报错。要改成启动探测的话注意会打 30 个请求。
5. **catalog 缓存会冻结** —— 30 分钟 TTL 实际不生效（`model.for_auth` 只在插件加载时
   被调一次），上新模型必须重启 CPA。详见第 4 节「缓存冻结」。
6. **CCR 那边 60 个模型仍未识别** —— 只修了 `deepseek-v4.1-flash` 和 `step-5-preview`。
   真值表见第 7 节，工具是 `~/.claude-code-router/patch-ccr-models.py`。

---

## 10. 快速自检清单

改完代码后：

```bash
./build-and-deploy.sh                        # 全流程,7 步全 ok 才算过
docker logs --tail 30 cpa | grep -i plugin   # 确认 plugin registered
curl -fsS http://localhost:8317/v1/models -H "Authorization: Bearer $KEY" | python3 -m json.tool
```

步骤：前置检查 → 探测布局 → 备份产物 → 编译 → 校验 → 部署 → 验证模型列表。

**注意末步有两种不同的失败信号**，别混淆：

| 输出 | 含义 |
|---|---|
| `!! 拿不到 API key` | 脚本没解析出 key（配置格式变了？）→ 见下面的读 key 说明 |
| `!! 请求 .../v1/models 失败` | key 有，但 CPA 没起来或还没监听 |

`$KEY` 从容器配置读：

```bash
docker exec cpa sh -c \
  "grep -A5 -E '^[[:space:]]*api-keys:' /CLIProxyAPI/config.yaml | grep -oE '\"[^\"]+\"' | head -1 | tr -d '\"'"
```

> ⚠️ **不要用 `grep '^api-keys:'`（锚定行首）** —— CPA v8 把 key 挪进了 `access:` 段：
>
> ```yaml
> access:
>     api-keys:                          # ← 真 key 在这（有缩进）
>         - "85106cafe9df..."
>
> api-keys:                              # ← 另有一个空的顶层节
>     openai-compatibility: []
> ```
>
> 锚定行首会匹配到那个空节，提取不到东西，然后验证步骤被**静默跳过**（表现为
> `!! 拿不到 API key`，看起来像网络问题，实际是找错了地方）。
>
> 同理，`api-keys` 也可能是顶层块列表或顶层内联 `["xxx"]`。所以要匹配任意缩进层级
> 并放宽后续行数 —— 上面这条命令三种写法通吃。

端到端测一个模型：

```bash
curl -s http://localhost:8317/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"Say exactly: OK"}],"max_tokens":600}'
```

**`max_tokens` 给小了会得到空 `content`** —— 思考型模型（hy3 系列、kimi-k3 等）会把预算
全花在 `reasoning_content` 上。测的时候至少给 600。
