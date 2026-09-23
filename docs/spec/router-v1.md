# cn-llm-router v1 规格（router-v1）

> 状态：draft → accepted（2026-09-23，四轮 grill 共识后落定）
> 关联 ADR：0001（评分口径与选择策略）、0002（LLM 判类分类器）、0003（自建薄网关）、0004（数据源与计算边界）、0005（供应商可用性过滤）

## 1. 定位与范围

把自然语言请求（或 Sub-Agent 子任务）识别为 **任务类别（12）× 复杂度（3 级）**，依据评分矩阵与三档策略确定性选出**主选/备选模型**，并通过 OpenAI 兼容网关路由到国产大模型。

- v1 范围：Python 库（`classify` / `select` / `route`）+ 可选 OpenAI 兼容薄网关；CLI 后置 v2。
- 不做：成本预算/计费监控（LiteLLM provider 预留）、飞书自动同步（backlog）、分类准确率评测工具（backlog）、PyPI 发布（后置）。

## 2. 数据层（唯一真相源 = `data/*.csv`，ADR-0004）

| 文件 | 内容 | 说明 |
|---|---|---|
| `data/models.csv` | 模型注册表 | logical_name / vendor / version / open_source / license / context_window / price_in / price_out / cache_price / platform / architecture / source_url / as_of |
| `data/scores.csv` | 能力分长表 | category × complexity × model × score（score 缺失 = 该模型该格无公开数据，不参与排序）；basis（评分依据）；as_of |
| `data/weights.csv` | 维度权重 | category × 8 维度 × weight；note="直接用VLM分" 表示该类别不经维度加权、直接用 VLM 分（多模态理解） |
| `data/categories.yaml` | 类别定义唯一事实源 | 12 类的 id/name/description/examples，分类器 prompt 渲染与文档共享 |
| `data/VERSION` | 数据版本 | `v1-20260923` |

代码加载：`utf-8-sig` 兼容；加载后断言模型集合、类别集合、12×3×15 覆盖率；任一断言失败即报错拒绝启动（数据不静默降级）。

## 3. 库 API

```python
classify(prompt: str, *, debug: bool = False) -> Classification
select(category: str, complexity: str, *, strategy: str = "平衡",
       availability_filter: bool | None = None) -> Recommendation
route(prompt: str, *, strategy: str = "平衡", debug: bool = False) -> RouteResult
```

- `Classification`：`{category, complexity, confidence, second_guess, low_confidence, fallback_reason, raw(仅debug)}`
- `Recommendation`：`{strategy, availability_filtered, primary: ModelChoice, backup: ModelChoice|None, notice: list[str]}`
- `ModelChoice`：`{logical_name, vendor, score, cost, rank, reason}`
- `RouteResult`：`{request, classification, recommendation, client: RouterClient}`
  - `client` 懒加载；`route(prompt).client.chat.completions.create(...)` 即 OpenAI 兼容调用，失败自动切备选（见 §6）。
- Sub-Agent 分步换模型 = 多次 `route()`，无需专门接口。

## 4. 分类器（ADR-0002）

- LLM 结构化判类：prompt 由 `categories.yaml` 渲染（12 类描述 + 示例），`response_format={"type":"json_object"}`，输出 `{category, complexity, confidence, second_guess}`。
- 分类模型候选链：`config/classifier.yaml` 的 `models: [逻辑名…]`，按供应商可用性取第一个；默认 `[GLM-5.3-Flash, DeepSeek-V4-Flash-0731]`。
- 降级链（均标 `low_confidence=true` + `fallback_reason`，不静默）：
  1. 规则关键词兜底：内置关键词表（程序编码/前端/文档/数据/数学/agent/长文/翻译/图像/问答/创意/代码审查…），命中明确关键词直接定类；
  2. 默认值兜底：`知识问答/检索` + `中`。
- LLM 返回的 category/complexity 不在 12 类 / 3 级内 → 按降级链处理；`confidence` 缺失按 0.5。

## 5. 选择器（ADR-0001/0005）

评分：`score(category, complexity, model)` 取自 `scores.csv`；缺失则模型不参与该格排序（notice 提示）。
成本：`cost = price_in×0.6 + price_out×0.4`；`cost_index = cost / min(cost of 参与排序的模型)`。

三档策略（确定性，文档即算法）：

| 策略 | 排序键 | 说明 |
|---|---|---|
| 纯能力优先 | `(score desc, cost asc)` | 成本权重=0 |
| 性价比优先 | `(score/cost_index desc, score desc)` | score<50 门槛剔除 |
| 平衡（默认） | 低复杂度 `score/cost_index^0.5`；中复杂度 `score/cost_index^0.25`；高复杂度 `(score desc, cost asc)` | 低偏性价比、高偏能力 |

可用性过滤（ADR-0005）：
- 默认 `availability_filter=true`：仅保留 key 已配置（对应 `key_env` 环境变量非空）的模型参与排序。
- `availability_filter=false`（或配置 `selector.availability_filter: false`）：全量集排序（计划场景）。
- `notice`：若全量集首选 ≠ 可用集首选，附提示"配置 {key_env} 后可换 {logical_name}"。

备选（第 3 轮 Q5）：
- 排序第 2 名；若与主选同厂商（同 API 挂了全挂），顺延到下一厂商第 1 名；无可用备选则 `backup=None`。

## 6. 网关（ADR-0003）

- `config/providers.yaml`：逻辑模型名 → `{provider, base_url, api_model, key_env, timeout}`；`config/` 只存样例，key 一律从环境变量读取（仓库零密钥，`.env.example` 模板）。
- `RouterClient`：OpenAI 兼容（`openai` SDK），请求指向主选；失败（网络/5xx/超时/限流）自动切备选重试 1 次（`max_failover` 可配，`allow_failover=False` 时禁用——非幂等请求由调用方决定）。
- 最终失败抛 `RouteError`：`{message, cause, attempted: [模型名…]}`（结构化错误，ADR-0002 第 2 轮）。

## 7. 日志

- 结构化日志到 stdout（JSON 行），INFO：`{event, request_id, category, complexity, strategy, primary, backup, duration_ms}`。
- `debug` 级别：含 `raw_reason` 与完整输入。不自动落盘（调用方决定收集）。

## 8. 配置查找

优先级：显式参数 > `CN_LLM_ROUTER_CONFIG` 目录 > 仓库默认（`data/` + `config/`）。
可选覆盖：`config/weights.yaml`（覆盖默认权重）、`config/classifier.yaml`、`config/providers.yaml`。
零配置：clone 即用（`classify/select` 离线可用；`route` 需配 key）。

## 9. 测试与工程

- 测试资产：`tests/fixtures/golden_cases.yaml`（12 类 × 3 复杂度各若干条典型请求，标注期望类别/复杂度）。
- 离线测试（CI 必跑）：数据加载断言、选择器三档排序黄金用例、备选同厂商规避、可用性过滤、分类器降级链（mock LLM）、规则兜底。
- 在线评测（backlog）：`scripts/eval_classifier.py` 跑真实 LLM 打分。
- 工程：Python ≥3.10；依赖 `openai` + `pyyaml`；pytest；GitHub Actions 跑测试。PyPI 后置。
