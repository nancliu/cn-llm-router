"""使用统计与成本报表测试（ADR-0011）。"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from cn_llm_router.stats import UsageRecorder, prompt_hash  # noqa: E402

import cost_report  # noqa: E402


# ---- 1. record 写入 JSONL 格式正确 ----

def test_record_writes_jsonl(tmp_path):
    log = tmp_path / "nested" / "usage.jsonl"  # 目录不存在，应自动创建
    rec = UsageRecorder(enabled=True, log_path=str(log))
    rec.record({
        "event_type": "classify",
        "category": "程序编码",
        "complexity": "低",
        "primary_model": "Qwen3.8-Max-0902",
        "estimated_cost_yuan": 0.001,
    })
    assert log.exists()
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["event_type"] == "classify"
    assert row["category"] == "程序编码"
    assert "timestamp" in row  # 自动补时间戳
    assert row["estimated_cost_yuan"] == pytest.approx(0.001)


# ---- 2. estimate_tokens 近似合理 ----

def test_estimate_tokens():
    assert UsageRecorder.estimate_tokens("") == 0
    # "hello world" 11 字符 → 11//4 = 2
    assert UsageRecorder.estimate_tokens("hello world") == 2
    # 至少 1
    assert UsageRecorder.estimate_tokens("ab") == 1


# ---- 3. estimate_cost 计算正确 ----

def test_estimate_cost():
    # 1000 输入×2 元/百万 + 500 输出×4 元/百万 = (2000+2000)/1e6 = 0.004 元
    cost = UsageRecorder.estimate_cost(1000, 500, 2.0, 4.0)
    assert cost == pytest.approx(0.004)
    # 缺价 → None
    assert UsageRecorder.estimate_cost(1000, 500, None, 4.0) is None
    assert UsageRecorder.estimate_cost(1000, 500, 2.0, None) is None


# ---- 4. stats_enabled=False 时不写文件 ----

def test_disabled_recorder_noop(tmp_path):
    log = tmp_path / "never.jsonl"
    rec = UsageRecorder(enabled=False, log_path=str(log))
    rec.record({"event_type": "classify", "category": "x"})
    assert not log.exists()


# ---- 5. cost_report 对构造的 JSONL 正确汇总 ----

def _write_log(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_cost_report_summarize(tmp_path):
    log = tmp_path / "usage.jsonl"
    _write_log(log, [
        # 2026-09：两次 classify（模型 A，各 0.001 元）
        {"timestamp": "2026-09-01T10:00:00", "event_type": "classify",
         "primary_model": "ModelA", "category": "程序编码", "estimated_cost_yuan": 0.001},
        {"timestamp": "2026-09-02T10:00:00", "event_type": "classify",
         "primary_model": "ModelA", "category": "程序编码", "estimated_cost_yuan": 0.001},
        # 2026-09：一次 completion（模型 B，0.01 元）
        {"timestamp": "2026-09-03T10:00:00", "event_type": "completion",
         "primary_model": "ModelB", "category": "", "estimated_cost_yuan": 0.01},
        # 2026-08：应被月份过滤排除
        {"timestamp": "2026-08-15T10:00:00", "event_type": "completion",
         "primary_model": "ModelB", "category": "", "estimated_cost_yuan": 9.99},
    ])
    events = cost_report.load_events(str(log))
    sel = cost_report.filter_month(events, "2026-09")
    s = cost_report.summarize(sel)
    assert s["total_events"] == 3
    assert s["total_cost_yuan"] == pytest.approx(0.012)
    by_model = {r["name"]: r for r in s["by_model"]}
    assert by_model["ModelA"]["count"] == 2
    assert by_model["ModelB"]["cost_yuan"] == pytest.approx(0.01)
    by_type = {r["name"]: r["count"] for r in s["by_event_type"]}
    assert by_type == {"classify": 2, "completion": 1}


def test_cost_report_missing_log_ok(tmp_path):
    """无日志文件时正常返回空，不抛异常。"""
    assert cost_report.load_events(str(tmp_path / "nope.jsonl")) == []


# ---- 6. prompt_hash 不泄露完整 prompt ----

def test_prompt_hash_privacy():
    secret = "这是一段机密的 prompt 内容，不应该被记录原文"
    h = prompt_hash(secret)
    assert len(h) == 8
    assert h != secret
    assert secret not in h
    assert all(c in "0123456789abcdef" for c in h)


# ---- 7. 集成：route() 开启统计后写 route 事件（离线，不调 LLM） ----

def test_route_writes_stats_event(data, cfg, tmp_path):
    from cn_llm_router import route
    from cn_llm_router.config import RouterConfig

    log = tmp_path / "usage.jsonl"
    offline = RouterConfig(
        data_dir=cfg.data_dir,
        config_dir=cfg.config_dir,
        providers={},
        classifier_models=cfg.classifier_models,
        availability_filter=False,
        cache_enabled=False,
        stats_enabled=True,
        stats_log_path=str(log),
    )
    res = route("写一个Python函数解析JSON", strategy="平衡", availability_filter=False, config=offline)
    assert res.recommendation.primary is not None
    assert log.exists()
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    route_events = [r for r in rows if r["event_type"] == "route"]
    assert len(route_events) == 1
    ev = route_events[0]
    assert ev["primary_model"] == res.recommendation.primary.logical_name
    assert ev["category"] == "程序编码"  # 规则兜底
    assert ev["estimated_input_tokens"] >= 1
    assert "写一个Python函数解析JSON" not in log.read_text(encoding="utf-8")  # 不记录原文
