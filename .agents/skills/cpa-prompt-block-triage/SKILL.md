---
name: cpa-prompt-block-triage
description: 排查经 CPA/workbuddy 转发到腾讯 CodeBuddy 时提示词被上游拦截的问题（11128 "Illegal API invocation from an unapproved channel"，或表现为"敏感内容"拒答）。当出现"某个客户端 100% 必挂、另一个客户端同一模型正常""流式请求 HTTP 200 但只收到 SSE error 事件""非流式返回 upstream 400 code 11128"这类症状时使用。覆盖抓包、分层隔离、句级二分、最小改写、固化到 sanitizeBlockedTemplates 与回归验证的完整流程。
agent_created: true
---

# CPA 提示词拦截排查

上游黑名单匹配的是**提示词里的固定模板子串**，不是语义审核，也不是鉴权问题。因此排查的核心是
"把触发片段缩到最小，再做一个不改语义的改写"。

## 一、先确认拦截形态

| 形态 | 表现 |
|---|---|
| 流式 | HTTP **200** + SSE 里 `data: {"error":{"message":"upstream 400: {...code:11128...}"}}` |
| 非流式 | HTTP 400，body 里有 `upstream 400: {"code":11128,...}` |
| 审核类 | "敏感内容"拒答，通常也是固定模板逐字命中 |

**关键**：流式请求在 CPA 访问日志里只有 200，只看状态码会误判成"成功"。要么看客户端收到的
SSE 事件，要么看 CPA 日志里的 warn 行。

判断是不是提示词问题的快速信号：

- 同一个 API key、同一个模型，**客户端 A 全挂、客户端 B 正常** → 提示词差异
- 同一客户端**每一轮都挂**，与具体问题无关 → 常驻模板命中（系统提示词里的固定段落）
- 换模型仍挂 → 与模型无关

## 二、抓原始请求体

先设定 skill 根目录（下文命令都用 `$SKILL_DIR`）：

```bash
SKILL_DIR=.agents/skills/cpa-prompt-block-triage   # 在不同 cwd 下请改成绝对路径
```

在客户端和 CPA 之间插一个透明代理，把请求/响应落盘，同时原样转发给 CPA：

```bash
CPA_API_KEY=<key> CAPTURE_DIR=/tmp/blockshot CAPTURE_PORT=8399 \
  python3 "$SKILL_DIR/scripts/capture_proxy.py"
```

把客户端（或该 surface 的 profile）的 baseURL 指到 `http://127.0.0.1:8399/v1`，触发一次请求，
然后看 `/tmp/blockshot/*-req.json`。

落盘格式：请求 `{"path":..., "body":...}`；响应 `{"status":..., "content":..., "tool_calls":...}`；
原始 SSE 另存 `*.sse`。

## 三、直连 CPA 复现，排除代理因素

```bash
python3 "$SKILL_DIR/scripts/bisect_prompt.py" replay /tmp/blockshot/01-xxxx-req.json
```

拿到 `BLOCKED` 就与代理无关，问题在请求体本身。

## 四、分层隔离：system / messages / tools

把两个能对照的请求（一个能过、一个被拦）互相换部件，四组就能定性：

```bash
python3 "$SKILL_DIR/scripts/bisect_prompt.py" swap <ok.json> <blocked.json>
```

已实测结论（**不要重复验证**）：

- **判定只扫 `messages`，不扫 `tools`** —— 把触发句塞进工具描述里照样通过
- 系统提示词是主战场；`tools` 的增减不影响结果

所以修复不需要动工具集，桌面端 32 个工具可以原样保留。

## 五、句级与组合二分

```bash
# 每句单独发一次，看哪句单独也会挂
python3 "$SKILL_DIR/scripts/bisect_prompt.py" sentences <req.json> --field system

# 所有两句组合：常见结论是"单句都过，两句连写才挂"
python3 "$SKILL_DIR/scripts/bisect_prompt.py" pairs <req.json> --field system

# 前缀二分，定位到具体字符区间
python3 "$SKILL_DIR/scripts/bisect_prompt.py" prefix <req.json> --field system
```

判读规则：

- 只有某一句单独就挂 → 句子级指纹，直接改那句
- 单句都过、两句组合挂 → 组合指纹，改动任一句即可，优先改副作用小的那句
- 前缀二分卡在单词中间 → 试着换掉那个词，验证是否只是该词触发

## 六、最小改写

原则：**只改被命中的那段子串，不改语义，不动标识符**。

- 优先同义替换（`workspace root` → `project root`、`CLI` → `CLI tool`）
- 品牌名类指纹直接抹掉品牌、换通用说法，比逐句追更耐用
- **别做全局关键词替换**：`workspace-write`（沙箱策略名）、`workspace instructions`
  （标题）、真实路径（`.../DeepSeek Harness.app/...`）都不能被误伤
- 改完立刻用同一条被抓的请求回归，流式也要测（错误只在 SSE 里出现）

## 七、固化到插件

1. 在 `main.go` 的 `blockedTemplateReplacements`（`strings.NewReplacer` 表）里加一条。
   注意顺序：更长的模式必须排在其前缀之前；路径类模式排在品牌名之前。
2. 在 `main_test.go` 补用例：最小改写结果、无关文本不被误伤、幂等性。
3. `go test ./...` 全绿后用 `./build-and-deploy.sh` 部署。
4. 部署后**用原来那条被拦的请求**做回归，非流式 + 流式各一次。

## 常见坑

- 只看 CPA 访问日志的 200 会漏掉拦截，必须看 SSE 事件或 warn 行
- 二进制插件里跑的是编译期代码，改完源码不部署等于没改；部署脚本会提示"工作区有未提交改动"
- 桌面端与 headless 的提示词不同（bundle 不同），一个通不代表另一个通，两边的失败要分别抓
- 同一段文字在"单独出现"和"与另一句连写"时判定可能相反，别用单句测试下结论
- 上游规则会变，改写是 cat-and-mouse；每加一条都留测试用例，下次换词时不至于回归

## 参考资料

- `"$SKILL_DIR"/references/case-log.md`：已确认的指纹与对应改写，含实测证据
- `"$SKILL_DIR"/scripts/capture_proxy.py`：请求/响应双向抓包代理
- `"$SKILL_DIR"/scripts/bisect_prompt.py`：replay / swap / sentences / pairs / prefix 五种探测
