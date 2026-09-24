"""社区/人工评测叠加测试（ADR-0009，第二数据源 LMArena Elo）。"""
import shutil
from pathlib import Path

import pytest

from cn_llm_router.config import RouterConfig, load_config
from cn_llm_router.data_loader import CategoryDef, ModelSpec, RouterData, load_data
from cn_llm_router.selector import select as _select


# ---- 1. 真实数据：加载与归一化 ----

def test_community_loaded_and_normalized(data):
    """Elo 1485 → (1485-1300)/250*100 = 74.0；原始值与来源留痕。"""
    assert data.community_scores["Kimi-K3"] == pytest.approx(74.0)
    assert data.community_scores["DeepSeek-V4-Flash-0731"] == pytest.approx((1436 - 1300) / 250 * 100)
    raw = data.community_raw["Kimi-K3"]
    assert raw["metric"] == "elo_overall"
    assert raw["value"] == 1485
    assert raw["source_url"].startswith("http")
    assert raw["as_of"] == "2026-09-13"


def test_community_models_subset_of_registered(data):
    """所有社区分模型都在 models.csv 注册。"""
    assert set(data.community_scores) <= set(data.models)


# ---- 2. 向后兼容：无 community_scores.csv 时为空 dict，行为不变 ----

def test_missing_community_csv_is_empty(data, cfg, tmp_path):
    """复制真实数据目录并删掉 community_scores.csv：加载不报错且 community 为空。"""
    dest = tmp_path / "nodata"
    shutil.copytree(cfg.data_dir, dest)
    (dest / "community_scores.csv").unlink()
    d2 = load_data(dest)
    assert d2.community_scores == {}
    assert d2.community_raw == {}
    # 行为与 v1 一致：纯 SuperCLUE 排序，GLM-5.3 仍是纯能力首选
    rec = _select(d2, "程序编码", "低", strategy="纯能力优先", cfg=cfg, availability_filter=False)
    assert rec.primary.logical_name == "GLM-5.3"


# ---- 3. 融合改变排序（构造 mock data） ----

def _mini_data() -> RouterData:
    """两个模型：A 纯 SuperCLUE 80 分但无社区分；B 78 分但社区分 95。"""
    d = RouterData()
    d.categories = {"知识问答/检索": CategoryDef(id="k", name="知识问答/检索", description="")}

    def spec(vendor: str) -> ModelSpec:
        return ModelSpec(
            logical_name=vendor, vendor=vendor, version="", open_source="", license="",
            context_window="", price_in=1.0, price_out=1.0, cache_price=0.0,
            platform="", architecture="", source_url="", as_of="",
        )

    d.models = {"ModelA": spec("VendorA"), "ModelB": spec("VendorB")}
    d.scores = {
        ("知识问答/检索", "高", "ModelA"): 80.0,
        ("知识问答/检索", "高", "ModelB"): 78.0,
    }
    return d


def test_blended_score_reorders_ranking():
    """无社区分 A(80) 本应赢；B 叠加社区分 95 后 blended=0.7*78+0.3*95=83.1 > 80，反超。"""
    d = _mini_data()
    d.community_scores = {"ModelB": 95.0}
    cfg = RouterConfig()  # 默认 α=0.7 β=0.3
    rec = _select(d, "知识问答/检索", "高", strategy="纯能力优先", cfg=cfg, availability_filter=False)
    assert rec.primary.logical_name == "ModelB"
    assert rec.primary.score == pytest.approx(0.7 * 78.0 + 0.3 * 95.0)
    assert "社区分叠加" in rec.primary.reason


def test_no_community_model_falls_back_with_notice():
    """无社区分的模型回退纯 SuperCLUE，且 notice 提示。"""
    d = _mini_data()
    d.community_scores = {"ModelB": 95.0}
    cfg = RouterConfig()
    rec = _select(d, "知识问答/检索", "高", strategy="纯能力优先", cfg=cfg, availability_filter=False)
    assert any("无社区评分" in n for n in rec.notice), f"notice 缺提示: {rec.notice}"


# ---- 4. α/β 配置覆盖 ----

def test_beta_zero_disables_blending():
    """β=0 时不融合，纯 SuperCLUE：A(80) 赢 B(78)，A 的 score 保持 80.0。"""
    d = _mini_data()
    d.community_scores = {"ModelB": 95.0}
    cfg = RouterConfig(community_alpha=1.0, community_beta=0.0)
    rec = _select(d, "知识问答/检索", "高", strategy="纯能力优先", cfg=cfg, availability_filter=False)
    assert rec.primary.logical_name == "ModelA"
    assert rec.primary.score == pytest.approx(80.0)
    assert "社区分叠加" not in rec.primary.reason


def test_selector_yaml_overrides_alpha_beta(tmp_path):
    """config/selector.yaml 的 community_alpha / community_beta 被读取。"""
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    (cdir / "selector.yaml").write_text(
        "community_alpha: 0.8\ncommunity_beta: 0.2\n", encoding="utf-8"
    )
    cfg = load_config(config_dir=str(cdir))
    assert cfg.community_alpha == pytest.approx(0.8)
    assert cfg.community_beta == pytest.approx(0.2)


# ---- 5. 未注册模型报错 ----

def test_unregistered_community_model_rejected(cfg, tmp_path):
    """community_scores.csv 出现未在 models.csv 注册的模型 → _validate 断言失败。"""
    dest = tmp_path / "baddata"
    shutil.copytree(cfg.data_dir, dest)
    with open(dest / "community_scores.csv", "a", encoding="utf-8") as f:
        f.write("Ghost-Model,lmarena,elo_overall,1400,https://example.com,2026-09-13\n")
    with pytest.raises(AssertionError, match="未注册模型"):
        load_data(dest)
