# ADR-0021：OpenAI 兼容 serve 网关（harness 接入）

- 状态：accepted
- 日期：2026-10-02
- 关联：ADR-0002（分类器）、ADR-0003（网关）、ADR-0005（可用性过滤）、ADR-0007（编排）、ADR-0010（缓存）、ADR-0017（Web 面板）、ADR-0019（国外对照）

## 背景

本 router 的初衷：脱离 Claude / GPT / Grok 等国外模型依赖，用国产大模型完成工作。
现有能力（classify / select / route / orchestrate）都是 **Python API + 出站 OpenAI 兼容客户端**，
外部 harness（Claude Code / Codex / 任意 OpenAI 客户端）无法直接消费——它们需要一个**入站的模型服务端点**。

接入目标（用户 2026-10-02 明确）：
1. **router 层不得造成能力变弱**：不改写 prompt、不吞参数、不截断流式、不重映射工具调用；router 只做"判类选型 + 完整转发"。
2. **充分发挥 harness 与国产模型自身能力**：harness 的工具循环 / 文件操作 / 上下文管理保留；模型全部国产。
3. 能力上限 = 国产模型自身水平（这是初衷接受的边界），router 不引入额外损耗。

Codex 原生使用 OpenAI Chat 格式（config.toml 可配自定义 `model_provider.base_url`），
**国产模型官方端点即 OpenAI 兼容——Codex 接入无需任何协议转换**，是零损耗路线。
Claude Code 需 Anthropic Messages 格式，走 LiteLLM 桥接（本 ADR 不实现，见"后续"）。

## 决策

新增 `cn_llm_router/serve.py`：标准库 `http.server`（零新依赖，与 ADR-0017 Web 面板同栈），
本地监听（默认 127.0.0.1:10041），暴露 OpenAI 兼容端点。

### 端点

| 端点 | 语义 |
|---|---|
| `POST /v1/chat/completions` | 主入口：判类选型 + 透明转发 |
| `GET /v1/models` | 列出可路由模型（可用 logical_name + `auto`） |
| `GET /health` | 存活检查 |

### 路由模式（请求体 `model` 字段）

- `model="auto"`（或缺省）：对请求内容判类（类别×复杂度）→ `select(strategy, availability_filter=True)` →
  按推荐主选/备选构建 RouterClient 转发。判类结果缓存（复用 ADR-0010 语义，1h）。
- `model="<logical_name>"`：判类仍执行（用于取类别上下文与备选），**主选强制为点名模型**，
  备选=同类别推荐中的备选（若与主选不同且可用）；点名模型未配 key → 400（`cause=missing_key`）。
  点名即用户意图，failover 只切到同类别备选，不擅自换主选之外的模型。
- 两种模式都保留 RouterClient 的失败自动切备选（仅网络/5xx/429/超时；401 不重试）。

### 透明转发铁律（本 ADR 的核心约束）

1. **不改写** messages / tools / tool_choice / system / 任何参数字段；
2. tools（JSON Schema）与多轮 tool_call / tool_result 原样往返；
3. 流式：上游 SSE chunk **逐块原样转发**（`data: <json>\n\n`，结束 `data: [DONE]`），不聚合、不改写、不重排；
4. 非流式：上游响应对象序列化为 JSON 原样返回；
5. 判类为**旁路动作**：用独立的内部分类调用（分类模型），不掺入被转发请求；
6. 上游最终失败 → OpenAI 风格错误 `{"error": {"message", "type": "router_error", "code": <cause>, "attempted": [...]}}`。

### 认证与安全

- 默认仅监听 127.0.0.1，不暴露公网；
- 可选 token：环境变量 `CN_LLM_ROUTER_SERVE_TOKEN` 或 CLI `--token`；设置后要求
  `Authorization: Bearer <token>` 或 `X-API-Key: <token>`，否则 401。

### 单实例守护（2026-10-03 增补）

多实例抢同一端口会造成路由混乱（曾出现双 serve / 双 litellm 并存）。
`run_server` 启动前自动检测：

- 端口空闲 → 正常启动；
- 端口被**本应用 serve 实例**占用（netstat 找 PID → 进程命令行匹配
  `cn_llm_router serve` / `cn-llm-router serve`）→ **自动关闭旧实例再启动新实例**；
- 端口被**其他程序**占用 → 直接报错退出，不自动处理（避免误杀）；
- CLI 新增 `--no-restart` 关闭自动重启。

检测基于标准库（socket bind 探测 + netstat/tasklist + PowerShell CIM），无新依赖。

### 判类输入提取

取请求 messages 中**最后一条 role=user 且 content 为字符串**的消息做判类（跳过 tool 消息），
避免工具结果 JSON 干扰；无则取最后一条 user 消息。判类哈希缓存 1h。

## 与现有模块关系

- `serve` 复用 `Classifier`（判类）、`selector.select`（选型）、`RouterClient`（转发 + failover）、
  `load_config/load_data`（配置与数据）——**不修改任何现有模块行为**；
- CLI 新增 `cn-llm-router serve --host --port --token [--strategy 平衡] [--no-restart]`；
- 与 ADR-0017 Web 面板（/route 推荐查询）互补：面板给人看推荐，serve 给 harness 发请求。

## 验证

- pytest `tests/test_serve.py`：本地 fake 上游（ThreadingHTTPServer）验证
  ① model=logical_name 透传（含 tools 原样到达上游、非流式响应一致）；
  ② stream=True 流式逐 chunk 透传（[DONE] 结束）；
  ③ model=auto 判类路由（规则兜底确定性）；
  ④ 点名模型缺 key → 400 missing_key；
  ⑤ /v1/models 与 /health；
  ⑥ 认证 token 生效（401）。
- 全量 pytest 无回归（不触碰 classify/select/route/gateway 行为）。

## 后续（不在本 ADR 范围）

- Claude Code 接入：litellm proxy 桥接（Anthropic⇄OpenAI），需实测 tool_use/tool_call 多轮往返；
- 会话级路由状态（serve 内锁定模型）v1 不做：harness 会话固定模型由"启动时 select 查好 logical_name 写进配置"达成；
- 请求级并发/限流/配额：v1 仅靠 ThreadingHTTPServer 多线程，不做限流。
