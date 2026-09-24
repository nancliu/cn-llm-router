"""Sub-Agent 多模型分配编排层（ADR-0007）。

把一组性质不同的子任务各自路由到合适的模型：对每个子任务独立完成
"判类 → 选模型 → 构建 client"，config/data 只加载一次并在子任务间共享。

本层只负责**分配模型与 client**，不启动任何 LLM 调用、不实现子任务执行引擎，
也不处理子任务间依赖（v1 为平铺子任务列表）。实际执行由调用方负责。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .classifier import Classifier
from .config import RouterConfig, load_config
from .data_loader import RouterData, load_data
from .gateway import RouterClient
from .selector import select as _select
from .types import DEFAULT_COMPLEXITY, Classification, Recommendation


@dataclass
class SubTaskSpec:
    """单个子任务的编排规格。

    - ``id``：子任务唯一标识，重复即抛 ``ValueError``。
    - ``description``：子任务自然语言描述；``category`` 缺省时据此判类。
    - ``category``：显式指定任务类别；缺省时自动 ``classify(description)``。
    - ``complexity``：显式复杂度（低/中/高）；``category`` 已给但本项缺省时取默认"中"。
    - ``strategy``：子任务级策略（纯能力优先/平衡/性价比优先）；缺省继承编排全局策略。
    """

    id: str
    description: str
    category: Optional[str] = None
    complexity: Optional[str] = None
    strategy: Optional[str] = None


@dataclass
class SubTaskRoute:
    """单个子任务的路由结果：规格 + 判类 + 推荐 + 网关 client。"""

    spec: SubTaskSpec
    classification: Classification
    recommendation: Recommendation
    client: Any  # RouterClient（懒加载，失败切备选，见 gateway.py）


@dataclass
class OrchestrationResult:
    """整组子任务的编排结果。"""

    subtasks: list[SubTaskRoute]
    strategy: str
    availability_filtered: bool


def orchestrate(
    subtasks: list[SubTaskSpec],
    *,
    strategy: str = "平衡",
    availability_filter: bool = True,
    debug: bool = False,
    config: RouterConfig | None = None,
) -> OrchestrationResult:
    """为一组子任务各自分配模型与网关 client（ADR-0007）。

    config/data 只加载一次，所有子任务共享。每个子任务：

    1. ``category`` 缺省 → ``classify(description)`` 同时定类与复杂度；
    2. ``category`` 已给但 ``complexity`` 缺省 → 复杂度取默认"中"；
    3. ``strategy`` 子任务级覆盖全局，缺省用本函数的 ``strategy``；
    4. 经确定性选择器得到 ``Recommendation``，并构建懒加载 ``RouterClient``。

    Args:
        subtasks: 子任务规格列表；为空或 ``id`` 重复时抛 ``ValueError``。
        strategy: 全局默认策略（纯能力优先/平衡/性价比优先）。
        availability_filter: 是否按供应商 key 可用性过滤推荐（默认开，与 route 一致）。
        debug: 透传给分类器，输出判类原始信息。
        config: 显式传入的配置；缺省走 ``load_config()``。

    Returns:
        OrchestrationResult：每个子任务一条 ``SubTaskRoute``。
    """
    if not subtasks:
        raise ValueError("orchestrate 需要至少一个子任务（subtasks 为空）。")

    seen: set[str] = set()
    for st in subtasks:
        if st.id in seen:
            raise ValueError(f"子任务 id 重复: {st.id!r}")
        seen.add(st.id)

    cfg: RouterConfig = config or load_config()
    data: RouterData = load_data(cfg.data_dir, weights_override=cfg.weights_override)
    classifier = Classifier(data=data, cfg=cfg, debug=debug)

    routes: list[SubTaskRoute] = []
    for st in subtasks:
        # 1) 解析 category / complexity
        if st.category is None:
            clf = classifier.classify(st.description)
            category = clf.category
            complexity = clf.complexity
        else:
            category = st.category
            complexity = st.complexity or DEFAULT_COMPLEXITY
            clf = Classification(
                category=category,
                complexity=complexity,
                confidence=1.0,
                low_confidence=False,
                fallback_reason=(
                    "子任务显式指定 category；复杂度缺省取中"
                    if st.complexity is None
                    else "子任务显式指定 category/complexity"
                ),
            )

        # 2) 子任务级策略覆盖全局
        eff_strategy = st.strategy or strategy

        # 3) 确定性选择 + 懒加载 client（复用 route 同款内部组件）
        rec = _select(
            data,
            category,
            complexity,
            strategy=eff_strategy,
            cfg=cfg,
            availability_filter=availability_filter,
        )
        client = RouterClient(rec, cfg)
        routes.append(SubTaskRoute(spec=st, classification=clf, recommendation=rec, client=client))

    return OrchestrationResult(
        subtasks=routes,
        strategy=strategy,
        availability_filtered=availability_filter,
    )
