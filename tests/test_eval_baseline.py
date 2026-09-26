"""评测基线门禁测试（ADR-0013）：不真实调 LLM，只测 check_baseline 纯函数。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import eval_classifier as ev  # noqa: E402


def _res(model: str, acc: float) -> dict:
    return {"model": model, "end_to_end_accuracy": acc, "n": 180}


def test_all_pass_above_threshold():
    results = [
        _res("Qwen3.8-Max-0902", 0.994),
        _res("DeepSeek-V4.1-Flash-CED", 0.989),
        _res("豆包Seed-2.1-Pro", 0.989),
    ]
    available = set(ev.EVAL_BASELINE)
    ok, messages = ev.check_baseline(results, available)
    assert ok is True
    joined = "\n".join(messages)
    assert "Qwen3.8-Max-0902" in joined
    assert "通过" in joined
    assert "跳过" not in joined


def test_below_threshold_blocks():
    results = [_res("Qwen3.8-Max-0902", 0.95)]  # 低于 0.97
    ok, messages = ev.check_baseline(
        results,
        available={"Qwen3.8-Max-0902", "DeepSeek-V4.1-Flash-CED", "豆包Seed-2.1-Pro"},
    )
    assert ok is False
    joined = "\n".join(messages)
    assert "低于门禁阈值" in joined
    # 其余两个基线模型未跑结果，也应判失败
    assert "未产出评测结果" in joined


def test_missing_key_skipped_not_failure():
    results = [_res("Qwen3.8-Max-0902", 0.994)]
    # 只配了 Qwen 的 key；DeepSeek 与豆包缺 key → 明确跳过，不误报
    ok, messages = ev.check_baseline(results, available={"Qwen3.8-Max-0902"})
    assert ok is True
    joined = "\n".join(messages)
    assert "未配置 key，跳过：DeepSeek-V4.1-Flash-CED" in joined
    assert "未配置 key，跳过：豆包Seed-2.1-Pro" in joined
    assert "拦截" not in joined


def test_threshold_edge_boundary():
    # 恰好等于阈值 → 通过
    results = [_res("Qwen3.8-Max-0902", ev.EVAL_THRESHOLD)]
    ok, _ = ev.check_baseline(results, available={"Qwen3.8-Max-0902"})
    assert ok is True
    # 差一丝 → 拦截
    results2 = [_res("Qwen3.8-Max-0902", ev.EVAL_THRESHOLD - 0.001)]
    ok2, _ = ev.check_baseline(results2, available={"Qwen3.8-Max-0902"})
    assert ok2 is False


def test_custom_model_not_in_baseline_ignored():
    # --models 自定义跑出的模型不在基线表里：低准确率也不参与阈值判定
    results = [_res("GLM-5.3-Flash", 0.50)]
    ok, messages = ev.check_baseline(results, available={"GLM-5.3-Flash"})
    assert ok is True  # 三个基线模型均"未配置 key 跳过"，无失败项
    assert any("跳过" in m for m in messages)
