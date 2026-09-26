"""VLM 真实实测分接入测试（ADR-0015）。"""
import shutil

import pytest

from cn_llm_router.data_loader import CategoryDef, ModelSpec, RouterData, load_data
from cn_llm_router.selector import select as _select


# ---- 1. 真实数据：加载与模型注册约束 ----

def test_vlm_scores_loaded(data):
    """data/vlm_scores.csv 中 Qwen/CED 的实测分已加载，且模型 ⊆ models。"""
    assert data.vlm_scores, "vlm_scores 不应为空（P1-4 已实测 Qwen/CED）"
    qwen = [k for k in data.vlm_scores if k[2] == "Qwen3.8-Max-0902"]
    ced = [k for k in data.vlm_scores if k[2] == "DeepSeek-V4.1-Flash-CED"]
    assert qwen, "Qwen3.8-Max-0902 应有 VLM 实测分"
    assert ced, "DeepSeek-V4.1-Flash-CED 应有 VLM 实测分"
    # 全部条目 category=多模态理解，复杂度覆盖低/中/高，分数在 0-100
    for k, v in data.vlm_scores.items():
        assert k[0] == "多模态理解"
        assert k[1] in ("低", "中", "高")
        assert 0.0 <= v <= 100.0
    assert {k[2] for k in data.vlm_scores} <= set(data.models)


def test_missing_vlm_csv_is_empty(data, cfg, tmp_path):
    """删掉 vlm_scores.csv：加载不报错且为空 dict，行为与 v1 一致。"""
    dest = tmp_path / "nodata"
    shutil.copytree(cfg.data_dir, dest)
    (dest / "vlm_scores.csv").unlink()
    d2 = load_data(dest)
    assert d2.vlm_scores == {}
    # 非多模态类选择行为不受影响
    rec = _select(d2, "程序编码", "低", strategy="纯能力优先", cfg=cfg, availability_filter=False)
    assert rec.primary.logical_name == "GLM-5.3"


# ---- 2. 选择器：mm 类优先 VLM 实测分（构造 mini data） ----

def _mini_data() -> RouterData:
    """ModelA 文本代理分 60 但 VLM 实测 90；ModelB 文本代理分 80、无 VLM 实测。"""
    d = RouterData()
    d.categories = {"多模态理解": CategoryDef(id="mm", name="多模态理解", description="")}

    def spec(name: str, vendor: str = "V") -> ModelSpec:
        return ModelSpec(
            logical_name=name, vendor=vendor, version="", open_source="", license="",
            context_window="", price_in=1.0, price_out=1.0, cache_price=0.0,
            platform="", architecture="", source_url="", as_of="",
        )

    d.models = {"ModelA": spec("ModelA", "VendorA"), "ModelB": spec("ModelB", "VendorB")}
    d.scores = {
        ("多模态理解", "低", "ModelA"): 60.0,
        ("多模态理解", "低", "ModelB"): 80.0,
    }
    return d


def test_mm_prefers_vlm_score():
    """mm 类：A(代理60/实测90) 反超 B(代理80/无实测)，reason 标注 VLM 实测分。"""
    from cn_llm_router.config import RouterConfig

    d = _mini_data()
    d.vlm_scores = {("多模态理解", "低", "ModelA"): 90.0}
    rec = _select(d, "多模态理解", "低", strategy="纯能力优先",
                  cfg=RouterConfig(), availability_filter=False)
    assert rec.primary.logical_name == "ModelA"
    assert rec.primary.score == pytest.approx(90.0)
    assert "VLM实测分" in rec.primary.reason


def test_fallback_to_text_proxy_with_notice():
    """无 VLM 实测分的模型回退文本代理分，notice 有提示。"""
    from cn_llm_router.config import RouterConfig

    d = _mini_data()
    d.vlm_scores = {("多模态理解", "低", "ModelA"): 90.0}
    rec = _select(d, "多模态理解", "低", strategy="纯能力优先",
                  cfg=RouterConfig(), availability_filter=False)
    assert any("文本代理分" in n for n in rec.notice), f"notice 缺提示: {rec.notice}"
    # 次名 ModelB 仍按文本代理分 80 参与
    assert rec.backup.logical_name == "ModelB"
    assert "文本代理分" in rec.backup.reason


def test_non_mm_category_unaffected():
    """非多模态类：vlm_scores 被忽略，按 scores.csv 原文排序。"""
    from cn_llm_router.config import RouterConfig

    d = RouterData()
    d.categories = {"程序编码": CategoryDef(id="prog", name="程序编码", description="")}

    def spec(name: str) -> ModelSpec:
        return ModelSpec(
            logical_name=name, vendor="V", version="", open_source="", license="",
            context_window="", price_in=1.0, price_out=1.0, cache_price=0.0,
            platform="", architecture="", source_url="", as_of="",
        )

    d.models = {"ModelA": spec("ModelA"), "ModelB": spec("ModelB")}
    d.scores = {
        ("程序编码", "低", "ModelA"): 60.0,
        ("程序编码", "低", "ModelB"): 80.0,
    }
    # 即使塞了 mm 实测分，程序编码类也不应采用
    d.vlm_scores = {("多模态理解", "低", "ModelA"): 99.0}
    rec = _select(d, "程序编码", "低", strategy="纯能力优先",
                  cfg=RouterConfig(), availability_filter=False)
    assert rec.primary.logical_name == "ModelB"
    assert rec.primary.score == pytest.approx(80.0)
    assert "VLM实测分" not in rec.primary.reason


# ---- 3. 未注册模型拒绝入库 ----

def test_unregistered_vlm_model_rejected(cfg, tmp_path):
    """vlm_scores.csv 出现未注册模型 → _validate 断言失败。"""
    dest = tmp_path / "baddata"
    shutil.copytree(cfg.data_dir, dest)
    with open(dest / "vlm_scores.csv", "a", encoding="utf-8-sig", newline="") as f:
        f.write("Ghost,VLM,低,50.0,test,20,2026-09-27,x,2026-09-27\n")
    with pytest.raises(AssertionError, match="未注册模型"):
        load_data(dest)
