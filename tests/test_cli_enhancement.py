"""CLI 增强测试（ADR-0012）：cache-status / cost-report / --stats 开关。

沿用 tests/test_cli.py 的子进程冒烟模式：跑 `python -m cn_llm-router ...`，
隔离 .env，保证 classify/route 走确定性规则兜底（不真实调 LLM）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def run_cli(*args, cwd=None, env=None):
    full_env = dict(os.environ)
    full_env["CN_LLM_ROUTER_NO_DOTENV"] = "1"
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "cn_llm_router", *args],
        capture_output=True, text=True, cwd=cwd or REPO, env=full_env,
    )


# ---- 1. cache-status 子命令输出含 cache_enabled/ttl/max_size 字段 ----

def test_cache_status_json_fields():
    r = run_cli("cache-status", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out.keys()) >= {"cache_enabled", "cache_ttl", "cache_max_size", "cache_path"}
    assert isinstance(out["cache_enabled"], bool)
    assert isinstance(out["cache_ttl"], int)
    assert isinstance(out["cache_max_size"], int)


def test_cache_status_text_hint():
    r = run_cli("cache-status")
    assert r.returncode == 0, r.stderr
    assert "cache_enabled" in r.stdout
    assert "cache_ttl" in r.stdout
    assert "cache_max_size" in r.stdout
    assert "Classifier" in r.stdout  # 实例生命周期提示


# ---- 2. cost-report 对空/不存在日志文件正常退出不报错 ----

def test_cost_report_missing_log_ok(tmp_path):
    r = run_cli("cost-report", "--log", str(tmp_path / "nope.jsonl"))
    assert r.returncode == 0, r.stderr
    assert "未找到" in r.stdout


# ---- 3. route --stats 触发统计记录（临时 log_path） ----

def test_route_stats_writes_log(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    log = tmp_path / "usage.jsonl"
    # 通过 selector.yaml 把 stats_log_path 指到临时目录
    (cfg_dir / "selector.yaml").write_text(
        f'stats_log_path: "{log.as_posix()}"\n', encoding="utf-8"
    )
    r = run_cli("route", "--stats", "--no-availability-filter",
                "--config-dir", str(cfg_dir),
                "写一个Python函数解析JSON")
    assert r.returncode == 0, r.stderr
    assert log.exists(), f"统计日志未生成，stderr={r.stderr}"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    route_events = [e for e in rows if e["event_type"] == "route"]
    assert len(route_events) == 1
    assert route_events[0]["category"] == "程序编码"  # 规则兜底，离线确定性
    # 提示走 stderr，不污染 stdout
    assert "统计已记录到" in r.stderr


def test_route_without_stats_no_log(tmp_path):
    """不传 --stats 时默认关闭，不写日志文件。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    log = tmp_path / "usage.jsonl"
    (cfg_dir / "selector.yaml").write_text(
        f'stats_log_path: "{log.as_posix()}"\n', encoding="utf-8"
    )
    r = run_cli("route", "--no-availability-filter",
                "--config-dir", str(cfg_dir),
                "写一个Python函数解析JSON")
    assert r.returncode == 0, r.stderr
    assert not log.exists()  # stats_enabled 默认 False，未写


# ---- 4. cost-report --json 输出结构正确（构造临时 JSONL） ----

def test_cost_report_json_structure(tmp_path):
    log = tmp_path / "usage.jsonl"
    rows = [
        {"timestamp": "2026-09-01T10:00:00", "event_type": "route",
         "primary_model": "ModelA", "category": "程序编码", "estimated_cost_yuan": 0.002},
        {"timestamp": "2026-09-02T10:00:00", "event_type": "classify",
         "primary_model": "", "category": "数据分析", "estimated_cost_yuan": 0.001},
        # 2026-08 应被月份过滤排除
        {"timestamp": "2026-08-15T10:00:00", "event_type": "route",
         "primary_model": "ModelB", "category": "", "estimated_cost_yuan": 9.99},
    ]
    with open(log, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    r = run_cli("cost-report", "--log", str(log), "--month", "2026-09", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["month"] == "2026-09"
    assert out["total_events"] == 2
    assert out["total_cost_yuan"] == pytest.approx(0.003)
    assert {"by_event_type", "by_model", "by_category"} <= out.keys()
    # by_event_type 应含 route=1, classify=1
    by_type = {row["name"]: row["count"] for row in out["by_event_type"]}
    assert by_type == {"route": 1, "classify": 1}
