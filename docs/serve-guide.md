# OpenAI 兼容 serve 网关接入指南（ADR-0021）

把 cn-llm-router 暴露为本地 OpenAI 兼容模型端点，供 **Codex** / 任意 OpenAI 兼容客户端 / 后续
LiteLLM 桥接（Claude Code）作为模型上游消费。核心承诺（ADR-0021 透明转发铁律）：
**router 层不改写 prompt、不吞参数、不截断流式、tool_call 原样往返——能力不因 router 层变弱**；
模型 100% 国产，harness 的工具循环 / 文件操作 / 上下文管理能力全部保留。

## 1. 启动

**推荐：一键启动脚本**（Windows，自动处理 DATABASE_URL 冲突 + 单实例重启）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gateway.ps1
```

脚本做的事：① 清理当前进程的 `DATABASE_URL` 环境变量 → ② 启动 serve（单实例守护，自动关旧启新）→ ③ 启动 litellm（若使用 Claude Code 桥接）→ ④ 就绪探测。日志：`serve.out.log` / `serve.err.log` / `litellm.out.log` / `litellm.err.log`。

**隔天/掉线自愈（Windows 计划任务，2026-10-05 注册）**：

```powershell
schtasks /Query /TN "cn-llm-router-gateway-check"     # 每 10 分钟：检查 10041/4000，挂了自动拉起
schtasks /Query /TN "cn-llm-router-gateway-startup"   # 登录 Windows 时自动启动网关
# 撤销：
schtasks /Delete /TN "cn-llm-router-gateway-check" /F
schtasks /Delete /TN "cn-llm-router-gateway-startup" /F
```

serve/litellm 是普通进程，非 Windows 服务：关机/重启/终端会话清理都会导致进程消失（**隔天掉线的根因**）。计划任务 = 登录自启 + 每 10 分钟自愈；`check-gateway.ps1` 用锁文件防并发重复重启（120s 窗口）。

**仅 serve（无 litellm）**：

```bash
cn-llm-router serve                      # 默认 127.0.0.1:10041
cn-llm-router serve --port 10041 --strategy 纯能力优先
cn-llm-router serve --token my-secret    # 启用认证（或环境变量 CN_LLM_ROUTER_SERVE_TOKEN）
cn-llm-router serve --no-restart         # 端口已有本应用实例时不自动重启（默认自动关旧启新）
```

> **为什么 litellm 启动要清 DATABASE_URL**：litellm 的数据库（PostgreSQL）用于 virtual keys / spend tracking / budget 等代理级功能。它沿用 12-factor 通用环境变量名 `DATABASE_URL`（Django / Prisma / Heroku 生态同款），启动时若 config 未配置 `general_settings.database_url` 就回退读环境变量（源码 `proxy_server.py`：config 优先 → 无则 `get_secret("DATABASE_URL")`）。你机器上其他软件（如 PostgreSQL 安装）可能已设置它 → litellm 误连失败崩溃。本桥接场景不需要这些 DB 功能，所以确保启动环境里没有该变量即可。注意 litellm 的 database_url **只支持 `postgresql://`**，sqlite 会报 unsupported scheme。

**单实例守护（默认开启）**：启动前自动检查目标端口——若已被**本应用 serve 实例**占用
（如重复启动、上一会话残留），自动关闭旧实例再启动新实例，避免多实例抢端口导致路由混乱；
若端口被**其他程序**占用则直接报错退出，不自动处理。日志示例：

```text
检测到已有 cn-llm-router serve 实例（PID 36036）监听 127.0.0.1:10041，自动关闭旧实例后重启…
cn-llm-router serve 网关已启动：http://127.0.0.1:10041/v1  （Ctrl+C 退出）
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

## 2.1 查看当前/最近选中的模型

每次请求路由到哪个模型可通过两种方式实时查看：

**① `GET /v1/routes`**（最近 50 条，最新在前）：

```bash
curl http://127.0.0.1:10041/v1/routes
```

```json
{"object": "list", "total": 4, "data": [
  {"ts": "2026-10-03 11:55:59", "category": "知识问答/检索", "complexity": "低",
   "model": "DeepSeek-V4.1-Flash-CED", "api_model": "deepseek-v4.1-flash-ced",
   "strategy": "平衡", "mode": "auto", "elapsed_s": 3.18, "tokens": 99060}
]}
```

- `model` = 评分矩阵里的 logical_name；`api_model` = 上游实际调用的模型名；
- `mode`：`auto`（判类路由）/ `named`（点名透传）；`status=failed` 时含 `error` 与 `attempted`。

**② serve 控制台日志**（INFO 级，转发成功/失败都打印）：

```text
路由 2026-10-03 11:55:46|知识问答/检索|复杂度=低 → DeepSeek-V4.1-Flash-CED (api_model=deepseek-v4.1-flash-ced) 耗时=2.8s tokens=96655
```

Windows 下日志写在前台终端；后台运行可重定向到文件（`python -m cn_llm_router serve > serve.log 2>&1`）。

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

## 4. 接入 Claude Code（经 LiteLLM 桥接，已实测）

Claude Code 只接受 Anthropic Messages 格式，链路：**Claude Code → litellm proxy（`/v1/messages`）→ cn-llm-router serve（`/v1/responses`）→ 国产模型**。
litellm 完成 Anthropic⇄Responses 转换，serve 完成 Responses⇄chat 转换（上游国产模型只有 chat API）；两端转换层均已用真实模型实测多轮 tool 往返无损。

### 4.1 启动（两条命令）

```bash
cn-llm-router serve --port 10041            # router 网关（含 /v1/responses）
litellm --config config/litellm-proxy.example.yaml --port 4000
# ⚠️ Windows：若机器级环境变量 DATABASE_URL 存在，litellm 会尝试连该库，
#    启动前先 Remove-Item Env:DATABASE_URL 或设 $env:DATABASE_URL=$null
```

### 4.2 Claude Code 侧环境变量

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-router-bridge        # litellm master_key
export ANTHROPIC_MODEL=router-auto                  # 判类路由；或 router-ced / router-qwen 点名
export ANTHROPIC_SMALL_FAST_MODEL=router-auto
claude
```

### 4.3 model_name 语义

| litellm model_name | serve model | 行为 |
|---|---|---|
| `router-auto` | `auto` | 每请求判类（缓存 1h）→ 按策略选型转发 |
| `router-ced` | `DeepSeek-V4.1-Flash-CED` | 点名透传 + 同类别备选兜底 |
| `router-qwen` | `Qwen3.8-Max-0902` | 点名透传（需百炼套餐已开通该模型访问） |

### 4.4 实测结论（2026-10-02，`reports/litellm-bridge-20261002.json`）

- `router-auto` / `router-ced`：真实模型 get_weather 工具**两轮往返通过**——R1 返回 `tool_use`（含 id/name/input），R2 回传 `tool_result` 后基于结果给出完整中文回答；`tool_use` id 原样往返，转换层零报错。
- `router-qwen`：上游 403 `AccessDenied.Unpurchased`（百炼套餐未开通 qwen3.8-max 访问，账户限制，非 router 缺陷）。
- 修复项：① litellm 把 tool_use 拆成独立 `function_call` item，与 assistant 文本交错产生"tool_calls 后插入第二条 assistant"，火山 CED 会 400 拒绝——serve 已合并相邻 assistant 消息；② 判类文本支持 content 数组消息，tool_result JSON 不再干扰类别判定。
- 边界：responses 端点内部聚合上游后组装 SSE 事件（首 token 延迟略增，后续可改边收边转）；`input_image` 映射为 `image_url` 透传（多模态未实测）。

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
