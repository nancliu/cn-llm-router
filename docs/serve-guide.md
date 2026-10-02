# OpenAI 兼容 serve 网关接入指南（ADR-0021）

把 cn-llm-router 暴露为本地 OpenAI 兼容模型端点，供 **Codex** / 任意 OpenAI 兼容客户端 / 后续
LiteLLM 桥接（Claude Code）作为模型上游消费。核心承诺（ADR-0021 透明转发铁律）：
**router 层不改写 prompt、不吞参数、不截断流式、tool_call 原样往返——能力不因 router 层变弱**；
模型 100% 国产，harness 的工具循环 / 文件操作 / 上下文管理能力全部保留。

## 1. 启动

```bash
cn-llm-router serve                      # 默认 127.0.0.1:10041
cn-llm-router serve --port 10041 --strategy 纯能力优先
cn-llm-router serve --token my-secret    # 启用认证（或环境变量 CN_LLM_ROUTER_SERVE_TOKEN）
```

前置：`config/providers.yaml` 已配置目标模型端点 + 对应 API key（见 `docs/eval-setup.md` / `.env.example`）。
未配任何 key 时 `/v1/models` 只列出 `auto`，`auto` 路由会返回 400 `no_available_models`。

## 2. 两种路由模式（请求体 `model` 字段）

| `model` 值 | 行为 |
|---|---|
| `auto`（或缺省） | 对请求判类（类别×复杂度）→ 按策略选主选/备选 → 转发；判类结果缓存 1h |
| `<logical_name>`（如 `Qwen3.8-Max-0902`） | 主选强制为该模型（点名即意图），备选=同类别评分矩阵备选；失败仍自动切备选 |

- 判类提取**最后一条 user 文本**（跳过 tool 结果），避免多轮工具循环的 JSON 干扰判类。
- 点名模型未配置 key → 400 `missing_key`；未知模型 → 400 `unknown_model`。
- 上游最终失败 → 502 `upstream_failed`（含 `attempted` 列表）；网络/5xx/429/超时自动切备选，401 不重试。

## 3. 接入 Codex（推荐，零转换层）

Codex 原生使用 OpenAI Chat 格式，`config.toml` 支持自定义 `model_provider`，**无需任何协议转换**：

```toml
# ~/.codex/config.toml（或项目 .codex/config.toml）
model = "auto"
model_provider = "cn-llm-router"

[model_providers.cn-llm-router]
name = "cn-llm-router"
base_url = "http://127.0.0.1:10041/v1"
wire_api = "chat"
# 本地网关无外部 key；若 serve 启用了 --token，这里填该 token
env_key = "CN_LLM_ROUTER_UPSTREAM_KEY"
```

然后：

```bash
export CN_LLM_ROUTER_UPSTREAM_KEY=my-secret   # 与 serve --token 一致；未启用认证可省略
cn-llm-router serve &                          # 先启动网关
codex exec "重构这个模块并补测试"
```

**会话级固定模型（推荐给工具循环场景）**：Codex 的工具循环多轮请求会不断触发 `auto` 判类，
为保证会话内模型稳定，先查好模型再写死：

```bash
cn-llm-router select --category 程序编码 --complexity 高 --strategy 纯能力优先
# 主选: Kimi-K3 ...（示例）
```

把 `config.toml` 的 `model` 改成 `Kimi-K3`（serve 点名透传 + 同类别备选兜底）。

## 4. 接入 Claude Code（经 LiteLLM 桥接，后续迭代）

Claude Code 只接受 Anthropic Messages 格式，而国产模型提供 OpenAI 兼容端点——需要一层协议转换。
计划路径：litellm proxy（`/v1/messages`）→ cn-llm-router serve（作为 OpenAI 兼容上游）。
⚠️ 该路径存在协议转换层，tool_use⇄tool_calls 多轮往返需实测验证，尚未实现（见 ADR-0021「后续」）。

## 5. 验证

```bash
# 健康检查
curl http://127.0.0.1:10041/health
# 可用模型
curl http://127.0.0.1:10041/v1/models
# 点名透传（非流式）
curl -X POST http://127.0.0.1:10041/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "Qwen3.8-Max-0902", "messages": [{"role": "user", "content": "写一个 Python 函数"}]}'
# auto 判类路由（流式）
curl -N -X POST http://127.0.0.1:10041/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "把这段译文润色一下"}], "stream": true}'
```

## 6. 设计边界（v1）

- 会话状态不做：`auto` 每请求判类（缓存 1h）；会话内固定模型由"启动时 select 写死 model"达成。
- 无限流/配额：ThreadingHTTPServer 多线程直连上游。
- 转发不记 usage 统计（stats 由 CLI `route` / `--stats` 路径记录）。
- 仅监听 127.0.0.1；如需局域网使用自行权衡并配 `--token`。
