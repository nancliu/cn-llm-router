# Sub-Agent 多模型编排实战指南

> 对应 ADR-0007。本文面向"一个大任务要拆成多个性质不同的子任务、每个子任务想用最合适的国产模型"的场景，讲清楚编排层怎么用、以及怎么接进 Cursor / Claude Code / 豆包等 Agent 客户端。

## 1. 核心概念

编排层在 `cn_llm_router.orchestrator`，只做一件事：**给一组子任务各自完成"判类 → 选模型 → 构建 client"**，config/data 只加载一次、子任务间共享。它**不启动任何 LLM 调用、不实现子任务执行引擎、不处理子任务间依赖**（v1 是平铺子任务列表）。实际执行由你自己的 Agent 循环负责。

三个关键对象：

| 对象 | 作用 |
|---|---|
| `SubTaskSpec` | 单子任务规格：`id`（唯一）、`description`（自然语言）、可选 `category`/`complexity`/`strategy` |
| `orchestrate(subtasks, ...)` | 入口函数，返回 `OrchestrationResult` |
| `SubTaskRoute` | 每个子任务一条：`spec` + `classification` + `recommendation` + `client`（懒加载 `RouterClient`） |

子任务的判类/选模型规则：

1. `category` 缺省 → 按 `description` 自动 `classify()`（同时定类与复杂度）；
2. `category` 已给但 `complexity` 缺省 → 复杂度取默认"中"；
3. 子任务级 `strategy` 覆盖全局 `strategy`，缺省用全局值；
4. 经确定性选择器得到 `Recommendation`，并为该子任务**独立**构建一个 `RouterClient`。

> 关键点：每个子任务拿到的是**自己的 client**，主选/备选、failover 链彼此独立。编码子任务挂了不会影响文书子任务的 client。

## 2. 最小可运行示例（离线）

仓库自带 `examples/orchestrate_demo.py`，零 key 可跑（强制规则兜底分类、`availability_filter=False` 从全量集推荐）：

```bash
python examples/orchestrate_demo.py
```

它演示 3 个子任务（编码 / 文书 / 数据分析）各自分配不同策略与模型。核心代码：

```python
from cn_llm_router import SubTaskSpec, orchestrate

result = orchestrate(
    [
        SubTaskSpec(id="coding", description="写一个 Python 脚本……", strategy="纯能力优先"),
        SubTaskSpec(id="doc",    description="把本周进展写成周报……", strategy="性价比优先"),
        SubTaskSpec(id="data",   description="分析近半年销售月度趋势……", strategy="平衡"),
    ],
    strategy="平衡",             # 全局默认策略
    availability_filter=False,   # 离线演示；有 key 时建议 True（只推荐已配 key 的模型）
)

for r in result.subtasks:
    print(r.spec.id, "->", r.classification.category,
          r.recommendation.primary.logical_name)
```

## 3. 三种客户端集成方式

编排层本质是 Python API，但你通常把它嵌进某个 Agent 客户端。下面三种场景都能用同一份思路：**路由器选模型 → 拿到 OpenAI 兼容 client → 用它跑该子任务**。

### 3.1 Cursor 集成

Cursor 本身不通过 cn-llm-router 切换内置模型；典型用法是把路由器当成"子任务该用哪个模型/端点"的决策者，由你的脚本或 `.cursorrules` 引导 Cursor 调外部 API。

**方式 A：`.cursorrules` 片段（推荐）**

在项目根 `.cursorrules`（或 Cursor 的 Rules → Project Rules）加入：

```markdown
# 子任务模型路由约定
处理包含多个性质不同子任务的需求时，先不要一股脑用当前模型硬写：
1. 编码类子任务：用 cn-llm-router 选"纯能力优先"模型，质量优先；
2. 文书/周报/邮件类模板化子任务：选"性价比优先"模型，省钱；
3. 数据/分析/口径类子任务：选"平衡"模型。
查具体模型与端点：运行
   cn-llm-router select --category <类> --complexity <低|中|高> --strategy <档>
或在 Python 里 from cn_llm_router import SubTaskSpec, orchestrate。
不要在同一对话里硬塞进所有子任务；把重活交给对应模型的 OpenAI 兼容端点。
```

**方式 B：shell 钩子脚本**

写一个薄脚本 `scripts/route_subtask.py`，输入子任务描述、输出推荐模型的 base_url/key_env/api_model，Cursor 里通过 `! python scripts/route_subtask.py "……"` 调用后，按输出的端点发请求。

### 3.2 Claude Code 集成

Claude Code 支持 hooks 与 CLI 子命令。两种接法：

**方式 A：`UserPromptSubmit` / 自定义 slash 命令前先路由**

在 `.claude/settings.json` 的 hooks 里，或直接写一个 slash 命令 `~/.claude/commands/route.md`：

```markdown
---
description: 把当前子任务路由到合适的国产模型
---
运行 `cn-llm-router route "$ARGUMENTS" --json`，从 JSON 里读取
recommendation.primary.logical_name 与 client 就绪状态；
随后用该模型的 OpenAI 兼容端点（config/providers.yaml 里查 base_url）执行。
```

**方式 B：CLI 直查（脚本化）**

```bash
# 单条子任务：分类 + 推荐（--json 可被 jq/脚本消费）
cn-llm-router route "写一个 Python 函数解析 JSON" --json

# 已知类别/复杂度时直接推荐
cn-llm-router select --category 程序编码 --complexity 高 --strategy 纯能力优先 --json
```

> Claude Code 自身模型仍由其配置决定；cn-llm-router 在这里负责"外挂子任务该打哪个国产模型端点"，二者互补而非互斥。

### 3.3 豆包 / 通用 Agent（Python API）

自己写 Agent 循环时，`orchestrate()` 就是编排入口：

```python
from cn_llm_router import SubTaskSpec, orchestrate

def run_agent_plan(plan: list[dict]):
    routes = orchestrate(
        [SubTaskSpec(id=p["id"], description=p["desc"], strategy=p.get("strategy"))
         for p in plan],
        strategy="平衡",
        availability_filter=True,   # 有 key：只推荐已配 key 的模型
    )
    outputs = {}
    for r in routes.subtasks:
        resp = r.client.chat.completions.create(
            model=r.recommendation.primary.logical_name,
            messages=[{"role": "user", "content": r.spec.description}],
        )
        outputs[r.spec.id] = resp.choices[0].message.content
    return outputs
```

每个 `r.client` 都是独立的 OpenAI 兼容客户端：主选失败（网络/5xx/429/超时）自动切 `recommendation.backup`，401 等鉴权错误直抛不重试。

## 4. 完整可运行示例：编码 + 文书 + 数据分析

下面这段在**配好 key 后**可直接发请求；未配 key 时把 `availability_filter=False` 即可看到完整推荐（不发请求）。

```python
from cn_llm_router import SubTaskSpec, orchestrate

subtasks = [
    SubTaskSpec(
        id="coding",
        description="实现一个带重试的 HTTP 客户端，支持指数退避与超时",
        strategy="纯能力优先",          # 编码要质量
    ),
    SubTaskSpec(
        id="doc",
        description="把上面这段实现整理成对内技术周报，列改动点与风险",
        strategy="性价比优先",          # 文书模板化
    ),
    SubTaskSpec(
        id="data",
        description="统计最近 30 天该客户端的失败率，按错误类型分组出趋势",
        strategy="平衡",
    ),
]

result = orchestrate(subtasks, strategy="平衡", availability_filter=True)

for r in result.subtasks:
    print(f"[{r.spec.id}] {r.classification.category}/{r.classification.complexity} "
          f"-> {r.recommendation.primary.logical_name}")
    # 实际调用（需已配对应 key）：
    # resp = r.client.chat.completions.create(
    #     model=r.recommendation.primary.logical_name,
    #     messages=[{"role": "user", "content": r.spec.description}],
    # )
    # print(r.spec.id, resp.choices[0].message.content)
```

子任务级 `strategy` 的作用：编码子任务覆盖为"纯能力优先"挑最强模型，文书覆盖为"性价比优先"挑便宜快的模型，数据分析沿用全局"平衡"。

## 5. 注意事项

- **client 懒加载**：`orchestrate()` 只构建 client 对象，不发任何请求；第一次 `chat.completions.create(...)` 才真正连上游。离线演示可以安全打印推荐而不调用。
- **failover**：`RouterClient` 内置主选→备选切换（仅网络/5xx/429/超时；401 鉴权失败不重试），次数由 `config/selector.yaml` 的 `max_failover` 控制（默认 1，设 0 关闭）。
- **availability_filter**：默认 `True`，只从"已配 key 的模型"里推荐；`False` 时从全量集比较（离线看推荐用，但选中模型没 key 会在首次调用时抛 `RouteError(missing_key)`）。离线示例统一用 `False`。
- **判类离线兜底**：无 key 时分类走"规则关键词兜底 → 默认值"，结果标 `low_confidence=True` 与 `fallback_reason`，不会静默；想让示例零网络调用，可像 `examples/orchestrate_demo.py` 那样传入空 `classifier_models` 的 config。
- **缓存**：同一段 `description` 的判类结果会被缓存（ADR-0010，默认内存 1h），子任务描述反复复用时不重复判类。
- **子任务间无依赖**：v1 是平铺列表，不做 DAG 调度；子任务产出物之间的拼接/传参由你的 Agent 循环负责。
- **配置/数据只加载一次**：整组子任务共享同一份 `RouterConfig` 与 `RouterData`，重复 `orchestrate()` 才会重复加载；长驻 Agent 里建议缓存 `OrchestrationResult` 或复用 `config`。
