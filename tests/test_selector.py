"""选择器测试：三档策略排序、备选同厂商规避、可用性过滤与 notice（ADR-0001/0005）。"""
import os

import pytest

from cn_llm_router import select


# ---- 三档策略（与打分表 v1 结论一致） ----

def test_balance_low_prefers_cost_performance(data, cfg):
    """平衡-低：主选性价比档 GLM-5.3-Flash（综合成本 1.6 元）。"""
    rec = select("程序编码", "低", strategy="平衡", availability_filter=False, config=cfg)
    assert rec.primary.logical_name == "GLM-5.3-Flash"
    assert rec.primary.cost == pytest.approx(1.6)


def test_pure_ability_picks_highest_score(data, cfg):
    rec = select("程序编码", "低", strategy="纯能力优先", availability_filter=False, config=cfg)
    assert rec.primary.logical_name == "GLM-5.3"


def test_value_strategy_gates_low_score(data, cfg):
    rec = select("程序编码", "低", strategy="性价比优先", availability_filter=False, config=cfg)
    assert rec.primary.logical_name == "GLM-5.3-Flash"


def test_balance_high_prefers_ability(data, cfg):
    rec = select("程序编码", "高", strategy="平衡", availability_filter=False, config=cfg)
    assert rec.primary.logical_name == "GLM-5.3"


def test_math_reasoning_high(data, cfg):
    rec = select("数学推理", "高", strategy="纯能力优先", availability_filter=False, config=cfg)
    highs = {m: s for (c, cx, m), s in data.scores.items() if c == "数学推理" and cx == "高"}
    assert rec.primary.score == max(highs.values())


# ---- 备选与同厂商规避 ----

def test_backup_not_same_vendor(data, cfg):
    rec = select("程序编码", "低", strategy="平衡", availability_filter=False, config=cfg)
    assert rec.backup is not None
    assert rec.backup.vendor != rec.primary.vendor


def test_backup_differs_from_primary(data, cfg):
    rec = select("翻译润色", "中", strategy="纯能力优先", availability_filter=False, config=cfg)
    if rec.backup is not None:
        assert rec.backup.logical_name != rec.primary.logical_name


# ---- 可用性过滤（ADR-0005） ----

def _full_top(data, cfg, category, complexity, strategy):
    return select(category, complexity, strategy=strategy, availability_filter=False, config=cfg)


def test_availability_requires_keys(data, cfg, monkeypatch):
    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="无可选模型"):
        select("程序编码", "低", strategy="平衡", config=cfg)


def test_availability_limits_to_configured_vendor(data, cfg, monkeypatch):
    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    rec = select("程序编码", "低", strategy="平衡", config=cfg)
    # 可用集只含 zhipu 三家（GLM-5.2/5.3/5.3-Flash）
    zhipu_vendor = data.models["GLM-5.3-Flash"].vendor
    assert rec.primary.vendor == zhipu_vendor
    assert rec.availability_filtered is True


def test_availability_notice_when_full_top_skipped(data, cfg, monkeypatch):
    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    zhipu_vendor = data.models["GLM-5.3-Flash"].vendor
    # 扫描 12 类 × 3 复杂度，找一个"全量集首选不是 zhipu"的格子
    found = None
    for cat in data.categories:
        for cx in ("低", "中", "高"):
            try:
                rec_full = _full_top(data, cfg, cat, cx, "平衡")
            except ValueError:
                continue
            if rec_full.primary.vendor != zhipu_vendor:
                found = (cat, cx, rec_full)
                break
        if found:
            break
    assert found is not None, "测试前提：应存在全量首选非 zhipu 的格子"
    cat, cx, rec_full = found

    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    rec = select(cat, cx, strategy="平衡", config=cfg)
    assert rec.notice, "可用集首选被过滤时应给出 notice"
    assert rec_full.primary.logical_name in rec.notice[0]


def test_availability_filter_off_ignores_keys(data, cfg, monkeypatch):
    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    rec = select("程序编码", "低", strategy="平衡", availability_filter=False, config=cfg)
    assert rec.primary.logical_name == "GLM-5.3-Flash"
    assert rec.availability_filtered is False


# ---- 非法输入 ----

def test_unknown_category(data, cfg):
    with pytest.raises(ValueError, match="未知任务类别"):
        select("不存在类别", "低", config=cfg)


def test_unknown_complexity(data, cfg):
    with pytest.raises(ValueError, match="未知复杂度"):
        select("程序编码", "超高", config=cfg)


def test_unknown_strategy(data, cfg):
    with pytest.raises(ValueError, match="未知策略"):
        select("程序编码", "低", strategy="随机", availability_filter=False, config=cfg)


# ---- 窗口感知过滤（issue #3） ----

def test_parse_context_window():
    from cn_llm_router.selector import _parse_context_window

    assert _parse_context_window("1M tokens") == 1048576
    assert _parse_context_window("256K") == 262144
    assert _parse_context_window("1M tokens (max output 384K)") == 1048576
    # max input 优先于总窗口（更贴近真实可输入量）
    assert _parse_context_window("1M (max input 960K/output 64K)") == 983040
    assert _parse_context_window("待补充") is None
    assert _parse_context_window("") is None


def test_input_tokens_none_keeps_ordering(data, cfg):
    """向后兼容：不传 input_tokens 时排序与既有行为一致。"""
    a = select("程序编码", "低", strategy="平衡", availability_filter=False, config=cfg)
    b = select("程序编码", "低", strategy="平衡", availability_filter=False,
               config=cfg, input_tokens=None)
    assert a.primary.logical_name == b.primary.logical_name
    assert all("窗口不足" not in n for n in a.notice)


def test_small_window_model_filtered_at_large_input(data, cfg, monkeypatch):
    """300K 输入下，把主选 GLM-5.3-Flash 窗口改成 128K，它应被窗口过滤剔除。"""
    from cn_llm_router.selector import select as raw_select

    monkeypatch.setattr(data.models["GLM-5.3-Flash"], "context_window", "128K")
    rec = raw_select(data, "程序编码", "低", strategy="平衡", cfg=cfg,
                     availability_filter=False, input_tokens=300_000)
    assert rec.primary.logical_name != "GLM-5.3-Flash"
    assert any("窗口不足" in n for n in rec.notice)


def test_all_window_overflow_fallback_to_full(data, cfg, monkeypatch):
    """全部候选窗口不足时回退全集（保底不报错），notice 记录过滤数。"""
    from cn_llm_router.selector import select as raw_select

    for m in data.models.values():
        if "1M" in (m.context_window or ""):
            monkeypatch.setattr(m, "context_window", "128K")
    rec = raw_select(data, "程序编码", "低", strategy="平衡", cfg=cfg,
                    availability_filter=False, input_tokens=300_000)
    assert rec.primary.logical_name  # 回退后仍有主选
    assert any("过滤" in n for n in rec.notice)
