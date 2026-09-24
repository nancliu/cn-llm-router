"""数据加载（docs/spec/router-v1.md §2，ADR-0004）。

唯一真相源 data/*.csv：models / scores（长表）/ weights；
共享类别定义 data/categories.yaml；数据版本 data/VERSION。
加载失败或断言不通过即拒绝启动，数据不静默降级。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import yaml

COMPLEXITIES = ("低", "中", "高")

# LMArena Elo 归一化区间（ADR-0009）：1300→0，1550→100，clamp 到 [0,100]
ELO_MIN = 1300.0
ELO_MAX = 1550.0


@dataclass
class ModelSpec:
    logical_name: str
    vendor: str
    version: str
    open_source: str
    license: str
    context_window: str
    price_in: float | None
    price_out: float | None
    cache_price: float
    platform: str
    architecture: str
    source_url: str
    as_of: str

    @property
    def cost(self) -> float | None:
        """综合成本 = 输入×0.6 + 输出×0.4（元/百万 tokens，ADR-0001）；缺价返回 None。"""
        if self.price_in is None or self.price_out is None:
            return None
        return self.price_in * 0.6 + self.price_out * 0.4


@dataclass
class CategoryDef:
    id: str
    name: str
    description: str
    examples: list[str] = field(default_factory=list)


@dataclass
class RouterData:
    models: dict[str, ModelSpec] = field(default_factory=dict)          # logical_name → ModelSpec
    scores: dict[tuple[str, str, str], float] = field(default_factory=dict)  # (category, complexity, model) → score
    weights: dict[tuple[str, str], float] = field(default_factory=dict)  # (category, dimension) → weight
    weight_notes: dict[tuple[str, str], str] = field(default_factory=dict)
    categories: dict[str, CategoryDef] = field(default_factory=dict)    # category 名称 → CategoryDef
    version: str = ""
    # 社区/人工评测叠加（ADR-0009，第二数据源）：
    # community_scores: logical_name → 归一化后 0-100 分；community_raw: 原值+来源（可溯源留痕）
    community_scores: dict[str, float] = field(default_factory=dict)
    community_raw: dict[str, dict] = field(default_factory=dict)


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_data(data_dir: Path, weights_override: dict | None = None) -> RouterData:
    d = RouterData()
    d.version = Path(data_dir / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0].strip()

    # models
    for r in _read_csv(data_dir / "models.csv"):
        def _opt(v: str) -> float | None:
            s = (v or "").strip()
            return float(s) if s else None

        d.models[r["logical_name"]] = ModelSpec(
            logical_name=r["logical_name"],
            vendor=r["vendor"],
            version=r["version"],
            open_source=r["open_source"],
            license=r["license"],
            context_window=r["context_window"],
            price_in=_opt(r["price_in"]),
            price_out=_opt(r["price_out"]),
            cache_price=_opt(r["cache_price"]) or 0.0,
            platform=r["platform"],
            architecture=r["architecture"],
            source_url=r["source_url"],
            as_of=r["as_of"],
        )

    # scores（长表）
    for r in _read_csv(data_dir / "scores.csv"):
        if r["score"] in (None, ""):
            continue
        d.scores[(r["category"], r["complexity"], r["model"])] = float(r["score"])

    # weights（区块A；多模态理解 note=直接用VLM分）
    wrows = _read_csv(data_dir / "weights.csv")
    for r in wrows:
        key = (r["category"], r["dimension"])
        d.weights[key] = float(r["weight"])
        if r.get("note"):
            d.weight_notes[key] = r["note"]

    # weights 覆盖（config/weights.yaml，ADR-0001 权重口径可选覆盖入口）
    if weights_override:
        for cat, dims in weights_override.items():
            for dim, w in dims.items():
                d.weights[(cat, dim)] = float(w)

    # categories
    cat_doc = yaml.safe_load((data_dir / "categories.yaml").read_text(encoding="utf-8")) or {}
    for c in cat_doc.get("categories", []):
        d.categories[c["name"]] = CategoryDef(
            id=c["id"], name=c["name"], description=c["description"], examples=list(c.get("examples", []))
        )

    # community_scores（ADR-0009，第二数据源）：文件不存在则空 dict（向后兼容，不报错）
    cpath = data_dir / "community_scores.csv"
    if cpath.exists():
        for r in _read_csv(cpath):
            model = (r.get("model") or "").strip()
            if not model:
                continue
            elo = float(r["value"])
            # 归一化：Elo 线性映射到 0-100，clamp 到 [0,100]
            norm = (elo - ELO_MIN) / (ELO_MAX - ELO_MIN) * 100.0
            d.community_scores[model] = max(0.0, min(100.0, norm))
            d.community_raw[model] = {
                "source": r.get("source", ""),
                "metric": r.get("metric", ""),
                "value": elo,
                "source_url": r.get("source_url", ""),
                "as_of": r.get("as_of", ""),
            }

    _validate(d)
    return d


def _validate(d: RouterData) -> None:
    """数据完整性断言：不通过即拒绝启动。"""
    assert len(d.models) >= 15, f"模型数异常: {len(d.models)}（期望 ≥15）"
    assert len(d.categories) == 12, f"类别数异常: {len(d.categories)}（期望 12）"
    cats = set(d.categories)
    score_cats = {k[0] for k in d.scores}
    missing_cats = cats - score_cats
    assert not missing_cats, f"以下类别在 scores 中缺失: {missing_cats}"
    # 每个类别应覆盖 3 级复杂度
    for c in cats:
        levels = {k[1] for k in d.scores if k[0] == c}
        assert levels == set(COMPLEXITIES), f"类别 {c} 复杂度覆盖异常: {levels}"
    # weights 应覆盖 12×8
    wcats = {k[0] for k in d.weights}
    assert len(wcats) == 12, f"权重类别数异常: {len(wcats)}"
    for c in cats:
        dims = [k[1] for k in d.weights if k[0] == c]
        assert len(dims) == 8, f"类别 {c} 权重维度数异常: {len(dims)}"
    # 模型在 scores 中的集合 ⊆ models 中的集合（Seedream 图像模型文本任务全 N/A，允许无有效分）
    score_models = {k[2] for k in d.scores}
    assert score_models <= set(d.models), "scores 中出现未注册模型"
    # community_scores 中的模型 ⊆ models（ADR-0009：未注册模型即数据错配，拒绝启动）
    assert set(d.community_scores) <= set(d.models), (
        "community_scores 中出现未注册模型: " + str(sorted(set(d.community_scores) - set(d.models)))
    )
    no_score = sorted(set(d.models) - score_models)
    if no_score:
        import logging

        logging.getLogger("cn_llm_router.data_loader").info("无有效评分的模型（不参与排序）: %s", no_score)
