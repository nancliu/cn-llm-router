# ADR-0007：Sub-Agent 多模型分配编排层（orchestrate）

- 状态：accepted
- 日期：2026-09-24
- 关联：ADR-0001、ADR-0002、ADR-0003、ADR-0005

## 背景

现有公共 API 是单请求入口：

- `classify(prompt)` → 判类；
- `select(category, complexity, strategy=...)` → 选主选/备选；
- `route(prompt, strategy=...)` → classify → select → 单个 `RouterClient`。

`route()` 一次只返回**一个模型 client**。但在 Sub-Agent / multi-agent 场景中，一个任务常被拆成多个性质不同的子任务（例如：写代码、做摘要、润色译文），它们应路由到**不同模型**——编码用能力强的代码模型，摘要用长上下文模型，润色用性价比模型。现有 API 没有"一次接收一组子任务、为每个子任务独立分配模型与 client"的编排能力，调用方只能在业务层重复调用 `route()`，既重复加载 config/data，也无法把分配结果组织成一个结构化结果。

## 决策

新增编排层 `orchestrate()`，与 `classify/select/route` 并列，作为公共 API：

- 输入：子任务描述列表（`SubTaskSpec`），每项含 `id`、`description`，可选 `category`、`complexity`、`strategy`。
- 对每个子任务独立完成"判类 → 选模型 → 构建 client"，汇总为 `OrchestrationResult`。
- config/data **只加载一次**，所有子任务共享，避免重复 IO。

## 设计

### 子任务规格 SubTaskSpec

```text
id: str                       # 子任务唯一标识，重复即报错
description: str              # 子任务自然语言描述，缺省 category 时据此判类
category: str | None = None  # 显式指定类别；缺省时自动 classify(description)
complexity: str | None = None # 显式复杂度；缺省规则见下
strategy: str | None = None   # 子任务级策略，缺省继承 orchestrate 的全局 strategy
```

判类/补全规则（与 `route()` 的降级链一致，只是逐子任务执行）：

1. `category` 缺失 → 调用 `classify(description)`，同时得到 `category` 与 `complexity`；
2. `category` 已给但 `complexity` 缺失 → `complexity` 取默认"中"；
3. `strategy` 取子任务级覆盖，否则用 `orchestrate()` 的全局 `strategy`。

### 输出

```text
SubTaskRoute:
  spec: SubTaskSpec
  classification: Classification   # 即使 category 显式给定，也回填为对齐后的 Classification
  recommendation: Recommendation
  client: RouterClient            # 懒加载，失败切备选（与 route 一致）

OrchestrationResult:
  subtasks: list[SubTaskRoute]
  strategy: str                    # 全局默认策略
  availability_filtered: bool      # 本次是否按供应商可用性过滤
```

### 复用关系

`orchestrate()` 内部复用现有组件，**不重写判类/选择/网关逻辑**：

- 用 `Classifier(data, cfg).classify()` 做判类（复用 ADR-0002 的降级链）；
- 用 `selector.select(data, category, complexity, ...)` 做确定性选择（复用 ADR-0001/0005）；
- 用 `RouterClient(recommendation, cfg)` 构建网关 client（复用 ADR-0003 的懒加载与失败切备选）。

config/data 的一次性加载方式与 `route()` 内部 `_ensure()` 保持一致。

## 不做什么

- **不实现 sub-agent 的实际执行引擎**：`orchestrate()` 只负责"给每个子任务分配好模型与 client"，真正的并发/串行执行、子任务间数据传递、prompt 组装与结果聚合由调用方负责。本层不启动任何 LLM 调用。
- **不做 DAG 依赖**：v1 是**平铺子任务列表**，不支持子任务间依赖、拓扑排序、输入输出引用。依赖编排留给后续版本或调用方。
- **不修改 classify/select/route 的签名与行为**：它们仍是单请求入口；`orchestrate` 是新增的上层组合。
- **不做子任务级 availability_filter 开关**：过滤粒度在本次编排级统一设置，与 `route()` 默认行为一致（默认开）。

## 与现有 API 的关系

| API | 粒度 | 输出 client |
| --- | --- | --- |
| `classify` | 单请求判类 | 无 |
| `select` | 单格选模型 | 无 |
| `route` | 单请求一站式 | 1 个 |
| `orchestrate`（本 ADR） | 一组子任务 | 每子任务 1 个 |

`orchestrate` 不替代 `route`：单请求仍用 `route`；需要一次为多个异构子任务各配模型时用 `orchestrate`。

## 错误约定

- 子任务列表为空 → `ValueError`；
- 子任务 `id` 重复 → `ValueError`（便于调用方定位编排配置错误）；
- 单格子任务选择失败（如无可用模型）沿 `select()` 原有 `ValueError` 抛出，不在本层吞掉。
