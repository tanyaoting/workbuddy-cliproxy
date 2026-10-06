# workbuddy-cliproxy

把**腾讯 CodeBuddy**（`copilot.tencent.com`）封装成 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)(CPA)插件,任何支持 OpenAI / Anthropic 协议的客户端(Claude Code、Cursor、Cline、SDK……)都能直接调用 CodeBuddy 背后的模型。

对 [Sliverkiss/cpa-plugin](https://github.com/Sliverkiss/cpa-plugin) 公开 `workbuddy.so` 的 clean-room 逆向重写,补齐了源码与 x86_64 支持;workbuddy 的原始设计归属 Sliverkiss。

## 工作原理

在 CPA 里注册为 `workbuddy` provider:负责 CodeBuddy 扫码登录、token 刷新,并把请求转发到 `copilot.tencent.com/v2/chat/completions`。登录后凭据存为 `workbuddy.json`。

## 模型

模型清单从 CodeBuddy 控制面 `GET /v3/config` **动态获取**(官方客户端自己读的接口),
服务端上新模型后无需改代码即可生效,30 分钟刷新一次。当前账号实测可见 30 个,包括
`glm-5.3` · `glm-5.1` · `glm-5v-turbo` · `kimi-k3-1` · `minimax-m3` · `hy3` · `hy4-preview` ·
`space-bunny` · `deepseek-v4-pro` · `deepseek-v4.1-flash` 等。

拉取失败时退回内置兜底表。具体可用性以 CodeBuddy 账号权限为准。

> 实现细节、模型来源考证、以及为什么不能用客户端打包的 `product.json` 当来源,见
> [CLAUDE.md](CLAUDE.md) 第 4 节。

## 安装

**前置**:运行中的 CLIProxyAPI v7.2.x 或 v8.x(带 CGO / 插件支持;两代插件 ABI 均为 `1`,
互相兼容)、CodeBuddy 账号、Go 1.26+ 与 gcc;编译架构需与 CPA 实例一致(amd64 / arm64)。

```bash
git clone https://github.com/lovingfish/workbuddy-cliproxy.git
cd workbuddy-cliproxy
CGO_ENABLED=1 GOOS=linux GOARCH=amd64 \
  go build -buildmode=c-shared -o workbuddy.so .
```

产物:`.so`(Linux)/ `.dylib`(macOS)/ `.dll`(Windows)。放到 CPA 的 `plugins/` 目录,在 `config.yaml` 启用:

```yaml
plugins:
  enabled: true
  dir: "plugins"
  configs:
    workbuddy: { enabled: true, priority: 100 }
```

重启 CPA,日志出现 `plugin loaded ... plugin_id=workbuddy` 即成功,`GET /v1/models` 也能看到上面的模型。然后到 CPA 面板添加 workbuddy 凭据,扫码登录 CodeBuddy。

**交叉编译 arm64**(在 macOS / x86 上编 linux/arm64):cgo 需要目标平台的 C 工具链,
把上面命令的 `GOARCH` 改成 `arm64` 即可;本机没装交叉工具链时,用官方 golang 镜像编(不污染宿主机):

```bash
docker run --rm --platform linux/arm64 \
  -v "$PWD":/src -w /src \
  -e CGO_ENABLED=1 -e GOOS=linux -e GOARCH=arm64 \
  -e GOPATH=/tmp/gopath -e GOCACHE=/tmp/gocache \
  golang:1.26-bookworm \
  go build -buildmode=c-shared -o dist/workbuddy.so .
```

输出路径必须写成容器内相对路径(`dist/workbuddy.so`)。写成宿主机绝对路径看似成功,
实际会写进容器临时层并随 `--rm` 丢失,容器里还是旧插件。

镜像基线决定产物的 glibc 下限:`bookworm` 要求 glibc ≥ 2.34(Debian 12 / Ubuntu 22.04+),
部署到更老的系统(如 Ubuntu 20.04,glibc 2.31)需改用 `golang:1.21-bullseye`。
也可用 [zig](https://ziglang.org) 当 CC 交叉编译,glibc 下限可低至 2.17,
但要在宿主机装约 1.8GB 工具链。

验证产物:

```bash
file workbuddy.so                                      # 应为 ELF 64-bit LSB shared object, ARM aarch64
nm -D --defined-only workbuddy.so | grep -E ' T cliproxy'   # 应为 4 个导出符号
strings workbuddy.so | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1   # glibc 下限
```

### 一键编译并部署到运行中的 CPA

改完代码后,不想每次手动编译、拷贝、重启、验证,直接用仓库根目录的脚本:

```bash
./build-and-deploy.sh
```

它把整个流程串起来,约 15 秒完成(有缓存后 1~2 秒):

1. **前置检查**:docker 可用、目标容器在跑、编译镜像存在;自动探测容器架构(决定 `GOARCH`)与 glibc 版本
2. **探测布局**:找到 CPA 工作目录,决定插件该拷到哪
3. **备份**:上次的 `dist/workbuddy.so` 存到 `dist/.backup/`(没有就跳过)
4. **编译**:容器内 `go build -buildmode=c-shared`,不污染宿主机
5. **校验**:架构、导出符号(必须 4 个)、**产物 glibc 下限 ≤ 容器 glibc**、打印 sha256
6. **部署**:`docker cp` + 重启容器
7. **验证**:轮询等 `plugin registered` 日志,再查 `/v1/models` 确认模型在列

### 产物备份

编译会覆盖 `dist/workbuddy.so`,所以脚本先把它按时间戳存到 `dist/.backup/`,默认保留最近
5 份(`KEEP_BACKUPS` 可调,设 `0` 关闭)。首次构建或备份目录为空时跳过这一步。

```bash
ls dist/.backup/
# workbuddy.so.20261006-035900  workbuddy.so.20261006-035839  ...
```

`dist/` 整个在 `.gitignore` 里,备份不会被提交。回滚就是把某份拷回并重新部署。

> 脚本**不备份容器内的插件** —— 因为 `plugins/` 通常是 bind mount,文件本来就存在宿主机上,
> 且容器重建不丢。见下面「工作目录自动探测」。

### 工作目录自动探测

不同打包方式的 CPA 镜像目录布局不一样:官方镜像是 `/app`,部分第三方镜像(如
`eceasy/cli-proxy-api`)是 `/CLIProxyAPI`。脚本按顺序找 `config.yaml` + `plugins/`
同时存在的目录:

```
/CLIProxyAPI  /app  /data  /opt/cliproxyapi
```

都不满足时退而求其次,只要有 `config.yaml` 就用。探测结果会打印出来:

```
==> 前置检查
  ok 工作目录 /CLIProxyAPI(插件 -> /CLIProxyAPI/plugins/workbuddy.so)
```

探测不到会直接报错,此时用 `APP_DIR_OVERRIDE=/路径` 手动指定。

注意有些镜像把 `plugins/` 做成 bind mount 挂到宿主机(如
`~/.cli-proxy-api/plugins/`),插件文件实际存在宿主机上,换容器也不会丢。

可用环境变量覆盖默认值:

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CONTAINER` | `cpa` | 目标容器名 |
| `PLUGIN_REL` | `plugins/workbuddy.so` | 插件路径(相对工作目录) |
| `APP_DIR_OVERRIDE` | 空 | 手动指定 CPA 工作目录,跳过探测 |
| `GO_IMAGE` | `golang:1.26-bookworm` | 编译镜像(需先 `docker pull`) |
| `GOPROXY` | `https://goproxy.cn,direct` | 模块代理 |
| `GOPATH_VOL_BASE` | `workbuddy-gopath` | Go 模块缓存 volume 名(自动加架构后缀) |
| `GOCACHE_VOL_BASE` | `workbuddy-gocache` | Go 编译缓存 volume 名(自动加架构后缀) |
| `KEEP_BACKUPS` | `5` | `dist/.backup/` 保留份数,`0` 关闭备份 |
| `CPA_URL` | `http://localhost:8317` | 验证用地址 |
| `CPA_KEY` | 从容器 config.yaml 读 | API key |
| `SKIP_VERIFY` | `0` | 设为 `1` 跳过 API 验证 |

`GOARCH` 不用手填:脚本读容器 `uname -m` 自动决定,`aarch64`/`arm64` → `arm64`,
`x86_64`/`amd64` → `amd64`。

Go 的模块缓存与编译缓存挂在宿主机 volume(`workbuddy-gopath-<arch>` /
`workbuddy-gocache-<arch>`)上,跨次编译复用,不用每次重下依赖。按架构分开是为了
避免 amd64 与 arm64 的编译缓存互相污染。想清掉重来:

```bash
docker volume rm workbuddy-gopath-arm64 workbuddy-gocache-arm64
```

脚本备份的是**编译产物**(`dist/.backup/`),不备份容器内已部署的插件 —— 因为 `plugins/`
通常是 bind mount,那份文件就是宿主机上的 `~/.cli-proxy-api/plugins/workbuddy.so`,
容器重建也不丢。要额外留一份部署前的快照,自己拷:

```bash
cp ~/.cli-proxy-api/plugins/workbuddy.so ~/.cli-proxy-api/plugins/workbuddy.so.bak
# 出问题就拷回去,再 docker restart cpa
```

## 使用

CPA 默认端口 `8317`,API key 见 `config.yaml` 的 `api-keys`。

| 协议 | Base URL |
|------|----------|
| OpenAI | `http://<host>:8317/v1` |
| Anthropic | `http://<host>:8317`(不带 `/v1`,走 `x-api-key`) |

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://localhost:8317
export ANTHROPIC_API_KEY=<api-key>
export ANTHROPIC_MODEL=hy3-preview-agent
claude
```

```bash
# curl / OpenAI
curl http://localhost:8317/v1/chat/completions \
  -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  -d '{"model":"hy3-preview-agent","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

流式 / 非流式都支持;非流式请求会被内部转成流式再聚合(CodeBuddy 上游 `code 11101` 拒绝非流式)。

## Claude Code 兼容性

腾讯 CodeBuddy 的内容审核把 Claude Code 的两句固定 system 模板逐字加进了黑名单,命中即回"敏感内容"拒答:

- `You are Claude Code, Anthropic's official CLI for Claude.`(身份句)
- `Main branch (you will usually use this for PRs)`(git 注入句)

任何一字改动都绕过(精确匹配,非语义审核)。workbuddy 转发前会自动把这两句做最小改写(`CLI`→`CLI tool`、`Main branch`→`Default branch`),语义不变,Claude Code 照常工作。

属于 cat-and-mouse:腾讯哪天多加模板句,得跟着改 `sanitizeBlockedTemplates`。

## 思考模式

hy3 系列(`hy3` / `hy3-preview` / `hy3-preview-agent`)自动开最大思考:workbuddy 转发前强制 `reasoning_effort=high`,覆盖客户端任何设置。CodeBuddy 只对 `high` 真正开深度思考(`medium` / `max` / `xhigh` 等档位它直接忽略),所以这已是 hy3 能用的最高档。思考内容走 SSE 的 `delta.reasoning_content`,客户端要支持渲染思考块才看得到。

## 流式

真流式(async):转发上游时边读边通过 `host.stream.emit` 把每个 chunk 实时推给 CPA,客户端逐字收到(不是等收齐了一股脑)。hy3 几千字的思考过程也是实时流出的,不是憋半天再刷出来。

## License

MIT。
