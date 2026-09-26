"""模型选择器（ADR-0001/0005，docs/spec/router-v1.md §5）。

确定性算法：代码消费 data/*.csv（能力分 + 成本），按策略档位排序，不依赖飞书表格推荐区。
- 纯能力优先：score desc, cost asc
- 性价比优先：value = score / cost_index，score<50 门槛剔除
- 平衡：低复杂度 score/cost_index^0.5；中复杂度 score/cost_index^0.25；高复杂度 score desc, cost asc
可用性过滤（ADR-0005）：默认只从 key 已配置的模型里选；关闭时全量集比较（notice 提示被跳过的最优项）。
"""
from __future__ import annotations

import logging

from .config import RouterConfig, key_available
from .data_loader import RouterData
from .types import COMPLEXITIES, ModelChoice, Recommendation, STRATEGIES

logger = logging.getLogger("cn_llm_router.selector")

MIN_SCORE_GATE = 50.0  # 性价比优先门槛


def select(
    data: RouterData,
    category: str,
    complexity: str,
    *,
    strategy: str,
    cfg: RouterConfig,
    availability_filter: bool | None = None,
) -> Recommendation:
    if category not in data.categories:
        raise ValueError(f"未知任务类别: {category}（可用: {sorted(data.categories)}）")
    if complexity not in COMPLEXITIES:
        raise ValueError(f"未知复杂度: {complexity}（可用: {COMPLEXITIES}）")
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略: {strategy}（可用: {STRATEGIES}）")

    filter_on = cfg.availability_filter if availability_filter is None else availability_filter
    eligible, full_rank = _rank(data, category, complexity, strategy, filter_on, cfg)

    if not eligible:
        hint = (
            "该格无公开评分，或可用性过滤后无可用模型（未配置任何 API key）。"
            "如需从全量集比较，设置 availability_filter=False 或 config/selector.yaml 中 availability_filter: false。"
        )
        raise ValueError(f"无可选模型：类别={category} 复杂度={complexity} 策略={strategy}。{hint}")

    primary = eligible[0]
    backup = _pick_backup(eligible, primary, data)
    notice: list[str] = []
    if filter_on and full_rank and full_rank[0].logical_name != primary.logical_name:
        skipped = full_rank[0]
        prov = cfg.providers.get(skipped.logical_name)
        key_env = prov.key_env if prov else "?"
        notice.append(
            f"若配置 {key_env}，全量集首选为 {skipped.logical_name}（能力分 {skipped.score:.1f}）"
        )
    # 缺失评分提示
    n_missing = sum(
        1 for m in data.models if (category, complexity, m) not in data.scores
    )
    if n_missing:
        notice.append(f"该格 {n_missing} 个模型无公开评分，未参与排序")

    # 社区分叠加提示（ADR-0009）：有社区分数据集时，无社区分的候选模型回退纯 SuperCLUE
    if data.community_scores and cfg.community_beta > 0:
        ranked = [m for m in data.models if (category, complexity, m) in data.scores]
        no_comm = [m for m in ranked if m not in data.community_scores]
        if no_comm:
            notice.append(
                f"{len(no_comm)} 个模型无社区评分，使用纯 SuperCLUE 分（未叠加 LMArena 信号）"
            )

    # VLM 实测分提示（ADR-0015）：多模态类有实测分数据集时，无实测分的模型回退文本代理分
    if category == "多模态理解" and data.vlm_scores:
        ranked = [m for m in data.models
                  if (category, complexity, m) in data.scores
                  or (category, complexity, m) in data.vlm_scores]
        has_vlm = [m for m in ranked if (category, complexity, m) in data.vlm_scores]
        if has_vlm and len(has_vlm) < len(ranked):
            notice.append(
                f"{len(ranked) - len(has_vlm)} 个模型无 VLM 实测分，使用文本代理分"
            )

    return Recommendation(
        strategy=strategy,
        availability_filtered=filter_on,
        primary=primary,
        backup=backup,
        notice=notice,
    )


def _rank(
    data: RouterData, category: str, complexity: str, strategy: str, filter_on: bool, cfg: RouterConfig
) -> tuple[list[ModelChoice], list[ModelChoice]]:
    """返回 (可用集排序, 全量集排序)；全量集仅用于 notice 对照。"""
    candidates = []
    for logical, spec in data.models.items():
        score = data.scores.get((category, complexity, logical))
        # VLM 实测分（ADR-0015）：多模态类有实测分时替代文本代理分；
        # 无代理分但有实测分的模型也因此获得排序资格。
        vlm_used = False
        if category == "多模态理解":
            vlm = data.vlm_scores.get((category, complexity, logical))
            if vlm is not None:
                score = vlm
                vlm_used = True
        if score is None:
            continue
        cost = spec.cost
        if cost is None:
            # 无价目：纯能力档按 +inf 参与（排最后），其余档无法参与（缺成本口径）
            if strategy != "纯能力优先":
                continue
            cost = float("inf")
        # 社区分叠加（ADR-0009）：有社区分且 β>0 时 blended = α×superclue + β×community；
        # 无社区分或 β=0 时保持纯 SuperCLUE（comm=None 供 reason 标注）。
        # VLM 实测分不叠加社区分（直接测量，避免二次混入）。
        comm = data.community_scores.get(logical) if not vlm_used else None
        if comm is not None and cfg.community_beta > 0:
            score = cfg.community_alpha * score + cfg.community_beta * comm
        else:
            comm = None
        candidates.append((logical, spec.vendor, score, cost, comm, vlm_used))

    full = _sort(candidates, strategy, complexity)
    avail = [c for c in full if _available(c[0], cfg)] if filter_on else full
    return [_to_choice(c, i + 1, strategy, cfg) for i, c in enumerate(avail)], [
        _to_choice(c, i + 1, strategy, cfg) for i, c in enumerate(full)
    ]


def _available(logical: str, cfg: RouterConfig) -> bool:
    prov = cfg.providers.get(logical)
    if prov is None:
        return False  # 未声明 provider 视为不可调用
    return key_available(prov)


def _sort(
    candidates: list[tuple[str, str, float, float, float | None, bool]], strategy: str, complexity: str
) -> list[tuple[str, str, float, float, float | None, bool]]:
    """按策略与复杂度对 (logical, vendor, score, cost) 排序（降序）。"""
    min_cost = min(c[3] for c in candidates)
    cost_index = lambda c: c[3] / min_cost  # noqa: E731
    if strategy == "纯能力优先":
        return sorted(candidates, key=lambda c: (c[2], -c[3]), reverse=True)
    if strategy == "性价比优先":
        gated = [c for c in candidates if c[2] >= MIN_SCORE_GATE]
        if not gated:
            gated = candidates  # 全被门槛剔除时退回原集，避免空推荐
        return sorted(gated, key=lambda c: (c[2] / cost_index(c), c[2]), reverse=True)
    # 平衡：低偏性价比、中轻惩罚、高偏能力（docs/spec/router-v1.md §5）
    if complexity == "低":
        return sorted(candidates, key=lambda c: c[2] / (cost_index(c) ** 0.5), reverse=True)
    if complexity == "中":
        return sorted(candidates, key=lambda c: c[2] / (cost_index(c) ** 0.25), reverse=True)
    return sorted(candidates, key=lambda c: (c[2], -c[3]), reverse=True)


def _pick_backup(eligible: list[ModelChoice], primary: ModelChoice, data: RouterData) -> ModelChoice | None:
    """备选 = 排序第 2 名；与主选同厂商则顺延到下一厂商第 1 名（ADR 词汇表：主选/备选）。"""
    for c in eligible[1:]:
        if c.vendor != primary.vendor:
            return c
    return None


def _to_choice(item: tuple[str, str, float, float, float | None, bool], rank: int, strategy: str, cfg: RouterConfig) -> ModelChoice:
    logical, vendor, score, cost, comm, vlm_used = item
    reason = {
        "纯能力优先": f"能力分最高（{score:.1f}）",
        "性价比优先": f"性价比最优（能力分 {score:.1f} / 成本指数）",
        "平衡": "平衡档推荐（兼顾能力与成本）",
    }[strategy]
    if vlm_used:
        reason += "；VLM实测分"
    else:
        reason += "；文本代理分"
    if comm is not None:
        reason += f"；含社区分叠加(α={cfg.community_alpha:.1f},β={cfg.community_beta:.1f})"
    return ModelChoice(logical_name=logical, vendor=vendor, score=score, cost=cost, rank=rank, reason=reason)
