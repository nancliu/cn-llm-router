"""cn-llm-router：国内大模型选择器/路由器核心库。

把自然语言请求（或 Sub-Agent 子任务）识别为任务类别 × 复杂度，
依据评分矩阵与三档策略确定性选出主选/备选模型，并通过 OpenAI 兼容网关路由到国产大模型。

- classify / select / route 为公共 API（见 docs/spec/router-v1.md）
- 数据唯一真相源：data/*.csv（ADR-0004）
- 分类器：LLM 结构化判类（ADR-0002），网关：自建 OpenAI 兼容薄层（ADR-0003）
- 默认按供应商可用性过滤推荐，可关闭（ADR-0005）
"""
from __future__ import annotations

from .config import RouterConfig, load_config
from .data_loader import RouterData, load_data
from .types import (
    Classification,
    ModelChoice,
    Recommendation,
    RouteError,
    RouteResult,
)

__version__ = "0.1.0"
__all__ = [
    "classify",
    "select",
    "route",
    "load_config",
    "load_data",
    "RouterConfig",
    "RouterData",
    "Classification",
    "ModelChoice",
    "Recommendation",
    "RouteResult",
    "RouteError",
    "__version__",
]


def _ensure(config: RouterConfig | None) -> tuple[RouterConfig, RouterData]:
    cfg = config or load_config()
    return cfg, load_data(cfg.data_dir, weights_override=cfg.weights_override)


def classify(prompt: str, *, debug: bool = False, config: RouterConfig | None = None) -> Classification:
    """把自然语言请求判为 任务类别 × 复杂度（LLM 判类 + 规则/默认值降级链）。"""
    from .classifier import Classifier

    cfg, data = _ensure(config)
    return Classifier(data=data, cfg=cfg, debug=debug).classify(prompt)


def select(
    category: str,
    complexity: str,
    *,
    strategy: str = "平衡",
    availability_filter: bool | None = None,
    config: RouterConfig | None = None,
) -> Recommendation:
    """依据评分矩阵与策略档位选出主选/备选（确定性，不依赖飞书表格推荐区）。"""
    from .selector import select as _select

    cfg, data = _ensure(config)
    return _select(data, category, complexity, strategy=strategy, cfg=cfg, availability_filter=availability_filter)


def route(
    prompt: str,
    *,
    strategy: str = "平衡",
    debug: bool = False,
    availability_filter: bool = True,
    config: RouterConfig | None = None,
) -> RouteResult:
    """组合入口：classify → select → 网关客户端（懒加载，失败自动切备选）。

    availability_filter=True（默认）时仅从已配置 API key 的模型中推荐；
    设为 False 从全量集比较（离线可查推荐，但调用需自行配置 key）。
    """
    from .classifier import Classifier
    from .gateway import RouterClient
    from .selector import select as _select

    cfg, data = _ensure(config)
    clf = Classifier(data=data, cfg=cfg, debug=debug).classify(prompt)
    rec = _select(
        data, clf.category, clf.complexity, strategy=strategy, cfg=cfg,
        availability_filter=availability_filter,
    )
    client = RouterClient(rec, cfg=cfg)
    return RouteResult(request=prompt, classification=clf, recommendation=rec, client=client)
