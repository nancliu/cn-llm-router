# ADR-0011：使用统计与成本报表（JSONL 日志 + 月度账单脚本）

- 状态：accepted
- 日期：2026-09-25
- 关联：ADR-0001（综合成本口径 price_in×0.6 + price_out×0.4）、ADR-0003（OpenAI 兼容网关）、ADR-0010（分类缓存）

## 背景

本库的重度使用者（作者本人）在 Cursor 等 IDE 中常触达国产模型套餐用量上限，对 token 消耗与费用高度敏感：

- 不知道每次 `classify` / `route` 实际走了哪个模型、耗时多少、花了多少钱；
- 月底无法回答"这个月路由在哪些模型/类别上烧了多少钱"；
- 路由推荐阶段的估算成本与真实 completion 成本混在一起，无法对账。

需要在不改公共 API 签名的前提下，把每次路由决策与真实调用的用量落盘，再由离线脚本汇总成月度账单。

## 决策

**记录每次 classify / route / completion 的模型、类别、耗时、估算 token 与估算成本，以 JSONL 追加写日志；离线脚本按月汇总为账单。**

- 默认关闭（`stats_enabled=False`），避免无意写日志；开启后追加写 `stats_log_path`（默认 `reports/usage.jsonl`）。
- 统计记录是**旁路**：记录失败不影响主流程（record 内部吞异常，仅打 debug 日志）。
- 不修改 `classify()` / `select()` / `route()` / `orchestrate()` 的函数签名，行为差异仅由 `RouterConfig.stats_enabled` 控制。

## 记录字段

每条 JSONL 事件一行，字段：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | ISO8601（本地时区）事件时间 |
| `event_type` | `classify` / `route` / `completion` |
| `prompt_hash` | `sha256(prompt)[:8]`，不存原文（隐私） |
| `prompt_chars` | prompt 字符数（辅助估算，不泄露内容） |
| `category` | 任务类别（route/completion 事件回填分类结果） |
| `complexity` | 低/中/高 |
| `strategy` | 策略档位（纯能力优先/平衡/性价比优先），classify 事件为空 |
| `classifier_model` | 实际执行判类的模型名（规则/默认兜底时为空） |
| `primary_model` | route 推荐主选模型；completion 事件为实际命中模型 |
| `backup_model` | route 推荐备选模型（可为空） |
| `duration_ms` | 该阶段耗时（毫秒，单调时钟差值×1000） |
| `estimated_input_tokens` | 估算输入 token 数（见下） |
| `estimated_output_tokens` | 估算/实际输出 token 数 |
| `estimated_cost_yuan` | 估算或实际成本（元） |
| `cache_hit` | 是否命中分类缓存（ADR-0010；v1 无缓存实现时恒为 false） |

## 成本口径

与 ADR-0001 一致，单价来自 `data/models.csv` 的 `price_in` / `price_out`（元/百万 tokens）：

```
estimated_cost_yuan = (input_tokens × price_in + output_tokens × price_out) / 1_000_000
```

- `classify` 事件：用**分类模型**单价；
- `route` 事件：用**主选模型**单价估算推荐阶段成本（route 本身不调 LLM 生成正文，output 估算记 0）；
- `completion` 事件：用**实际命中模型**单价，token 数取自 OpenAI 响应的 `response.usage`（`prompt_tokens` / `completion_tokens`）——这是真实成本；
- 模型缺价（price_in/out 为空）时 `estimated_cost_yuan=null`，报表中归入"未知成本"。

## token 估算

无真实 usage 时（classify/route 推荐阶段）：

```
estimated_input_tokens  ≈ len(prompt + system_prompt) / 4
estimated_output_tokens ≈ 100   # classify 为定长 JSON；route 推荐阶段不计输出
```

`len/4` 是工程近似：英文约 4 char/token，中文约 1.5–2 char/token，混合文本折中取 4。**这是估算值**，仅用于趋势与月度量级参考；真实账单以 completion 事件的 `response.usage` 为准。

## 隐私

- 只记录 `prompt_hash`（SHA-256 前 8 位）与 `prompt_chars`，**不落盘完整 prompt / system_prompt / 响应内容**；
- hash 前 8 位用于同一会话内粗略去重，不构成反向恢复原文的手段。

## 不做什么

- 不做实时仪表盘/网页展示（v1 仅 JSONL + 命令行报表）；
- 不修改 `classify/select/route/orchestrate` 签名；
- 不记录完整 prompt 或响应正文；
- 不把统计成本回写到评分矩阵或推荐决策（统计是旁路，不反馈路由）；
- 不做日志轮转（v1 单文件追加，用户可自行 logrotate）。

## 影响

- 新增 `cn_llm_router/stats.py`：`UsageRecorder`（record / estimate_tokens / estimate_cost / prompt_hash）；
- `cn_llm_router/__init__.py`：`classify()` / `route()` 在 `stats_enabled=True` 时包计时并写事件；
- `cn_llm_router/gateway.py`：`RouterClient._call()` 成功返回前按 `response.usage` 记 completion 事件；
- 新增 `scripts/cost_report.py`：按月汇总 JSONL，输出终端表格与可选 `--json`；
- 新增 `tests/test_stats.py`。
