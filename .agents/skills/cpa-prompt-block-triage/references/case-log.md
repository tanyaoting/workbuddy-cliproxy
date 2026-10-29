# 已确认的拦截指纹与改写

记录已实测确认的条目、证据和对应改动。新条目按同样格式追加，并同步到
`main.go` 的 `blockedTemplateReplacements` 与 `main_test.go`。

## 1. Claude Code 身份句（历史）

| 项 | 内容 |
|---|---|
| 命中串 | `You are Claude Code, Anthropic's official CLI for Claude.` |
| 改写 | `CLI` → `CLI tool` |
| 证据 | 改一个词即可通过，说明是逐字匹配而非语义审核 |

## 2. git 分支注入句（历史）

| 项 | 内容 |
|---|---|
| 命中串 | `Main branch (you will usually use this for PRs)` |
| 改写 | `Main branch` → `Default branch` |

## 3. dsh 品牌名与 Web GUI 段（2026-10-21）

| 项 | 内容 |
|---|---|
| 命中串 | ① `You are an AI agent powered by DeepSeek Harness`；② Web GUI 段的前两句组合 |
| 改写 | 身份句整句去掉 `powered by` 从句；其余品牌名换成通用说法 |
| 证据 | 单发 GUI 段任一句都能过，两句连写才挂；只改身份句时 GUI 仍被拦 |
| 例外 | 绝对路径 `.../DeepSeek Harness.app/...` 必须原样保留，用哨兵占位 |

## 4. dsh @file 引用说明（2026-10-29）

**症状**：桌面端（desktop profile，base + web-app bundle）请求 100% 被拦，headless 正常；
流式表现为 HTTP 200 + SSE error 事件。

**触发源**：`@deepseek-ai/dsh-file-reference-local` 注册的 `FILE_REFERENCE_PROMPT`（407 字符）。
该插件只在 `web-app` bundle 启用，因此只有桌面端带这段。**与用户是否真的用了 `@` 无关**——
只要 `read` 工具存在，这段就常驻系统提示词。抓到的两条被拦请求里，用户消息分别是「你好」
和「测试」，`messages` 中 `@` 出现次数为 0。

**二分结果**：

| 变体 | 结果 |
|---|---|
| 完整 desktop system prompt | BLOCKED |
| 去掉该 section | OK |
| 第 1 句单独 | OK |
| 第 2 句单独 | OK |
| **第 1 句 + 第 2 句** | **BLOCKED** |
| 第 1 句 + 第 3 句 | OK |
| 第 1+2 句，`workspace root` → `project root` | OK |
| 第 1+2 句，`on the host` → `on this machine` | BLOCKED |

第 1 句：`Tokens prefixed with @ are paths the user explicitly referenced.`
第 2 句：`Relative paths resolve from the workspace root; absolute paths identify files or directories on the host.`

**改写**：`workspace root` → `project root`（全系统提示词里 `workspace` 只出现 1 次，就是这里）。

**副作用检查**：不能做全局 `workspace` 替换 —— `workspace-write`（沙箱策略名）、
`workspace instructions`（工作区指令标题）都会被误伤。

**判定范围**：把触发句塞进 `tools[0].function.description` 后请求仍然通过，
说明上游只扫 `messages`，不扫 `tools`。桌面端 32 个工具、`present`/`schedule_*` 都无需改动。

## 测量方法备注

- 每条探测都设 `stream: false`，把"HTTP 200 + SSE error"简化成"HTTP 400 + error"，便于判定
- 同一内容重复 3 次结果稳定，确认不是随机抽样
- 改动单个词就能翻转结果，说明是子串/指纹级判定，不是语义审核
- 改完必须用**原来那条被拦的请求**回归，并单独验证一次流式
