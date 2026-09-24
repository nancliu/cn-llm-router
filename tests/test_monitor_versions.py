"""scripts/monitor_versions.py 离线测试（ADR-0008）。

全部用 mock / 本地 HTML 字符串，不发真实网络请求。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "monitor_versions.py"
spec = importlib.util.spec_from_file_location("monitor_versions", SCRIPT)
mv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mv)


# ---------------------------------------------------------------------------
# 1. diff 逻辑
# ---------------------------------------------------------------------------
def test_diff_same_values_no_change():
    row = {"version": "2026-08-13", "price_in": "9.0", "price_out": "27.0",
           "context_window": "1M tokens"}
    extracted = {"version": "2026-08-13", "price_in": 9.0, "price_out": 27.0,
                 "context_window": "1M tokens", "unit_notes": {}}
    diffs = mv.diff_model(row, extracted)
    for field in mv.MONITORED_FIELDS:
        assert diffs[field]["status"] == mv.STATUS_NOCHANGE, field
    assert mv.overall_status(diffs, fetch_ok=True) == mv.STATUS_NOCHANGE


def test_diff_different_values_changed():
    row = {"version": "2026-08-13", "price_in": "9.0", "price_out": "27.0",
           "context_window": "1M tokens"}
    extracted = {"version": "2026-09-20", "price_in": 12.0, "price_out": 36.0,
                 "context_window": "2M tokens", "unit_notes": {}}
    diffs = mv.diff_model(row, extracted)
    changed = [f for f, d in diffs.items() if d["status"] == mv.STATUS_CHANGED]
    assert set(changed) == set(mv.MONITORED_FIELDS)
    assert mv.overall_status(diffs, fetch_ok=True) == mv.STATUS_CHANGED


def test_diff_fills_pending_old_value():
    """CSV 原值是'待补充'，抓到新值 → 视为补全（有变更）。"""
    row = {"version": "待补充", "price_in": "6.5", "price_out": "27.0",
           "context_window": "待补充"}
    extracted = {"version": "2026-07-16", "price_in": 6.5, "price_out": 27.0,
                 "context_window": "1M tokens", "unit_notes": {}}
    diffs = mv.diff_model(row, extracted)
    assert diffs["version"]["status"] == mv.STATUS_CHANGED
    assert diffs["context_window"]["status"] == mv.STATUS_CHANGED
    assert diffs["price_in"]["status"] == mv.STATUS_NOCHANGE


# ---------------------------------------------------------------------------
# 2. parse_page：从已知 HTML 片段提取价格/版本/上下文
# ---------------------------------------------------------------------------
DEEPSEEK_LIKE_HTML = """
<html><body>
<h1>DeepSeek 定价</h1>
<table>
<tr><td>输入价格（缓存未命中）</td><td>¥9.0 / 百万 tokens</td></tr>
<tr><td>输出价格</td><td>¥27.0 / 百万 tokens</td></tr>
</table>
<p>当前版本 2026-08-13，上下文窗口 1M tokens</p>
</body></html>
"""


def test_parse_page_extracts_prices():
    out = mv.parse_page(DEEPSEEK_LIKE_HTML)
    assert out["price_in"] == pytest.approx(9.0)
    assert out["price_out"] == pytest.approx(27.0)
    assert out["unit_notes"]["price_in"] is None  # 口径一致
    assert out["unit_notes"]["price_out"] is None


def test_parse_page_extracts_version_and_context():
    out = mv.parse_page(DEEPSEEK_LIKE_HTML)
    assert out["version"] == "2026-08-13"
    assert out["context_window"] == "1Mtokens" or out["context_window"].startswith("1M")


def test_parse_page_returns_nothing_on_blank():
    out = mv.parse_page("<html><body>nothing useful here</body></html>")
    assert out["price_in"] is None
    assert out["price_out"] is None
    assert out["version"] is None
    assert out["context_window"] is None


# ---------------------------------------------------------------------------
# 3. 抓取失败 → status=抓取失败，不抛异常
# ---------------------------------------------------------------------------
def test_process_model_fetch_failure(monkeypatch):
    def boom(url, dry_run=False):
        raise OSError("network down")
    # fetch_url 内部 try/except 已兜住；这里直接 monkeypatch 返回失败
    monkeypatch.setattr(mv, "fetch_url",
                        lambda url, dry_run=False: {"ok": False, "status_code": None,
                                                    "html": None,
                                                    "error": "URLError: network down"})
    row = {"logical_name": "Fake-Model", "vendor": "Fake", "source_url": "https://example.com/x",
           "version": "v1", "price_in": "1", "price_out": "2", "context_window": "1K"}
    rec = mv.process_model(row, dry_run=False)
    assert rec["status"] == mv.STATUS_FETCH_FAIL
    assert "network down" in (rec["error"] or "")
    assert rec["fields"] == {}  # 没走到 parse


def test_process_model_no_source_url():
    row = {"logical_name": "NoUrl-Model", "vendor": "Fake", "source_url": "",
           "version": "v1", "price_in": "1", "price_out": "2", "context_window": "1K"}
    rec = mv.process_model(row, dry_run=False)
    assert rec["status"] == mv.STATUS_FETCH_FAIL
    assert rec["error"] == "无 source_url"


# ---------------------------------------------------------------------------
# 4. dry-run 模式不调用 parse_page
# ---------------------------------------------------------------------------
def test_dry_run_does_not_parse(monkeypatch):
    def fake_fetch(url, dry_run=False):
        return {"ok": True, "status_code": 200, "html": "<html>pricing table</html>",
                "error": None}
    monkeypatch.setattr(mv, "fetch_url", fake_fetch)

    def boom_parse(html, row=None):
        raise AssertionError("dry-run 模式下不应调用 parse_page")
    monkeypatch.setattr(mv, "parse_page", boom_parse)

    row = {"logical_name": "Dry-Model", "vendor": "Fake",
           "source_url": "https://example.com/pricing",
           "version": "v1", "price_in": "1", "price_out": "2", "context_window": "1K"}
    rec = mv.process_model(row, dry_run=True)
    assert rec["status"] == mv.STATUS_REACHABLE
    assert rec["http_status"] == 200
    assert rec["fields"] == {}


# ---------------------------------------------------------------------------
# 5. JSON 报告结构完整
# ---------------------------------------------------------------------------
def test_generate_report_structure(tmp_path):
    results = [
        {"logical_name": "A", "vendor": "X", "source_url": "https://a",
         "fetched_at": "2026-09-24T10:00:00", "http_status": 200,
         "status": mv.STATUS_CHANGED, "error": None,
         "fields": {
             "version": {"old": "2026-01-01", "new": "2026-09-01",
                         "status": mv.STATUS_CHANGED, "writable": True, "note": None},
             "price_in": {"old": "9.0", "new": 12.0,
                          "status": mv.STATUS_CHANGED, "writable": True, "note": None},
             "price_out": {"old": "27.0", "new": 27.0,
                           "status": mv.STATUS_NOCHANGE, "writable": True, "note": None},
             "context_window": {"old": None, "new": None,
                                "status": mv.STATUS_PENDING, "writable": False, "note": None},
         }},
        {"logical_name": "B", "vendor": "Y", "source_url": "https://b",
         "fetched_at": "2026-09-24T10:00:01", "http_status": None,
         "status": mv.STATUS_FETCH_FAIL, "error": "URLError: timeout", "fields": {}},
    ]
    out = tmp_path / "report.json"
    report = mv.generate_report(results, out)

    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total"] == 2
    assert set(data.keys()) >= {"generated_at", "total", "summary", "models"}
    # 每个模型记录含必需字段
    for m in data["models"]:
        assert {"logical_name", "source_url", "fetched_at", "status", "fields"} <= set(m.keys())
    # 变更模型的 fields 里 old/new/status 齐全
    a = next(m for m in data["models"] if m["logical_name"] == "A")
    for field, info in a["fields"].items():
        assert {"old", "new", "status"} <= set(info.keys()), field
    assert data["summary"][mv.STATUS_CHANGED] == 1
    assert data["summary"][mv.STATUS_FETCH_FAIL] == 1


# ---------------------------------------------------------------------------
# 6. write_updated_csv：只写可写入的变更，不覆盖原文件
# ---------------------------------------------------------------------------
def test_write_updated_csv_only_writes_changes(tmp_path):
    csv_path = tmp_path / "models.csv"
    csv_path.write_text(
        "logical_name,vendor,version,price_in,price_out,context_window,source_url,as_of\n"
        "M1,V,2026-01-01,9.0,27.0,1M,https://a,2026-09-23\n"
        "M2,V,2026-02-02,1.0,2.0,256K,https://b,2026-09-23\n",
        encoding="utf-8-sig",
    )
    rows = mv.load_models(csv_path)
    results = [
        {"logical_name": "M1", "status": mv.STATUS_CHANGED, "fields": {
            "version": {"old": "2026-01-01", "new": "2026-09-01",
                        "status": mv.STATUS_CHANGED, "writable": True, "note": None},
            "price_in": {"old": "9.0", "new": 9.0,
                         "status": mv.STATUS_NOCHANGE, "writable": True, "note": None},
            "price_out": {"old": "27.0", "new": 27.0,
                          "status": mv.STATUS_NOCHANGE, "writable": True, "note": None},
            "context_window": {"old": "1M", "new": None,
                               "status": mv.STATUS_PENDING, "writable": False, "note": None},
        }},
        {"logical_name": "M2", "status": mv.STATUS_FETCH_FAIL, "fields": {}},
    ]
    new_path = tmp_path / "models.csv.new"
    mv.write_updated_csv(rows, results, new_path)
    assert new_path.exists()
    # 原文件未被覆盖
    assert csv_path.read_text(encoding="utf-8-sig").count("2026-09-23") == 2
    text = new_path.read_text(encoding="utf-8-sig")
    # M1 的 version 被更新；M2 保持原值（抓取失败不动）
    assert "2026-09-01" in text
    assert "2026-02-02" in text  # M2 version 保留
    # as_of 全部更新为今天
    today = __import__("datetime").date.today().isoformat()
    assert text.count(today) == 2
