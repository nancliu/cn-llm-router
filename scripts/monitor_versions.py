#!/usr/bin/env python3
"""模型版本监控：抓取厂商定价页，对比 data/models.csv 检测变更（ADR-0008）。

用法:
    python scripts/monitor_versions.py [--dry-run] [--update] [--output reports/version-monitor-<ts>.json]

- --dry-run: 只校验 source_url 可达性（HTTP 状态码），不做内容解析
- --update: 生成 data/models.csv.new（不覆盖原文件），as_of 更新为当天
- --output: JSON 报告路径，缺省 reports/version-monitor-<timestamp>.json

设计约束（ADR-0008）:
- 只监控 version / price_in / price_out / context_window 四个字段
- 抓不到标"待补充"，禁止编造
- 不直接覆盖 data/models.csv
- 单模型失败不影响整体；无网络时正常出报告（status=抓取失败）
- 仅用标准库 urllib，不引入第三方 HTTP 依赖
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "data" / "models.csv"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"

MONITORED_FIELDS = ("version", "price_in", "price_out", "context_window")

USER_AGENT = "cn-llm-router-monitor/0.1 (+version tracking; standard library urllib)"
TIMEOUT = 15

STATUS_NOCHANGE = "无变化"
STATUS_CHANGED = "有变更"
STATUS_FETCH_FAIL = "抓取失败"
STATUS_PENDING = "待补充"
STATUS_REACHABLE = "可达"  # 仅 dry-run 模式使用

# 监控字段在 CSV 里的类型提示：price_in/price_out 是数值
_NUMERIC_FIELDS = {"price_in", "price_out"}


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_models(csv_path: Path) -> list[dict]:
    """读取 data/models.csv（utf-8-sig），返回原始 dict 行。"""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def first_source_url(row: dict) -> str:
    """source_url 列多个 URL 用 ' ; ' 分隔，取第一个。"""
    raw = (row.get("source_url") or "").strip()
    if not raw:
        return ""
    return re.split(r"\s*;\s*", raw, maxsplit=1)[0].strip()


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def fetch_url(url: str, dry_run: bool = False) -> dict:
    """抓取 URL。返回 {ok, status_code, html, error}；任何异常都不抛出。"""
    if not url:
        return {"ok": False, "status_code": None, "html": None, "error": "无 source_url"}
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        if dry_run:
            # HEAD 优先；被拒则退化为 GET 但不读 body
            try:
                req.method = "HEAD"
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    code = getattr(resp, "status", resp.getcode())
                    return {"ok": 200 <= code < 400, "status_code": code, "html": None, "error": None}
            except Exception:
                req.method = "GET"
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    code = getattr(resp, "status", resp.getcode())
                    return {"ok": 200 <= code < 400, "status_code": code, "html": None, "error": None}
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code = getattr(resp, "status", resp.getcode())
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read()
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return {"ok": 200 <= code < 400, "status_code": code, "html": text, "error": None}
    except Exception as e:  # 网络/超时/HTTP 错误一律兜住
        return {"ok": False, "status_code": None, "html": None,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 页面解析（通用启发式 + 厂商特定 pattern；抓不到返回 None = 待补充）
# ---------------------------------------------------------------------------
def _strip_html(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&yen;", "¥").replace("&quot;", '"'))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _looks_cache(ctx: str) -> bool:
    return ("缓存" in ctx) or ("cache" in ctx.lower()) or ("命中" in ctx)


def _unit_note_after(text: str, end: int) -> str | None:
    """检查数字后面 30 字符内的单位；非'元/百万 tokens'口径则返回提示文案，否则 None。"""
    window = text[end:end + 30].lower()
    if ("百万" in window) or ("million" in window) or ("per million" in window) or ("/m" in window):
        return None
    if ("千" in window) or ("k tokens" in window) or ("/k" in window):
        return "单位疑似千 tokens，待人工换算"
    if ("$" in window) or ("美元" in window) or ("usd" in window):
        return "单位疑似美元，待人工换算"
    if "张" in window:
        return "单位疑似按张计费，待人工换算"
    # 页面本身是定价页，未标注单位时默认按百万 tokens 口径（与 CSV 一致）
    return None


def _extract_price(text: str, direction: str) -> tuple[float | None, str | None]:
    """direction: 'in' or 'out'。返回 (数值或None, 单位提示或None)。"""
    if direction == "in":
        labels = ["输入价格", "输入计费", "输入单价", "输入 Token", "Input price", "输入"]
    else:
        labels = ["输出价格", "输出计费", "输出单价", "输出 Token", "Output price", "输出"]
    # 第一遍：跳过缓存/cache 命中行（那是 cache_price，不是 price_in）
    for label in labels:
        for m in re.finditer(re.escape(label) + r"[^0-9¥$]{0,30}[¥$]?\s*(\d+(?:\.\d+)?)",
                             text, re.I):
            ctx = text[max(0, m.start() - 20):m.end() + 20]
            if _looks_cache(ctx):
                continue
            unit = _unit_note_after(text, m.end())
            return float(m.group(1)), unit
    # 第二遍：退化为任何匹配（可能含缓存行，留 unit_note 提示）
    for label in labels:
        m = re.search(re.escape(label) + r"[^0-9¥$]{0,30}[¥$]?\s*(\d+(?:\.\d+)?)",
                      text, re.I)
        if m:
            unit = _unit_note_after(text, m.end())
            return float(m.group(1)), unit
    return None, None


def _extract_context_window(text: str) -> str | None:
    patterns = [
        r"上下文窗口[^0-9A-Za-z]{0,10}(\d+(?:\.\d+)?\s*[MmKk]\s*(?:tokens?)?)",
        r"[Cc]ontext\s*[Ww]indow[^0-9A-Za-z]{0,10}(\d+(?:\.\d+)?\s*[MmKk]\s*(?:tokens?)?)",
        r"(\d+(?:\.\d+)?\s*[Mm]\s*tokens?)",
        r"(\d+(?:\.\d+)?\s*[Kk]\s*tokens?)",
        r"(\d+万\s*tokens?)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return re.sub(r"\s+", "", m.group(1))
    return None


def _extract_version(text: str) -> str | None:
    # 日期型：2026-08-13 / 2026年8月13日
    m = re.search(r"(20\d{2})[-年.](\d{1,2})[-月.](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 旬型：2026-09上旬 / 2026年9月下旬
    m = re.search(r"(20\d{2})[-年.](\d{1,2})\s*[月]?\s*(上旬|中旬|下旬)", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}{m.group(3)}"
    # 版本号型：V4.1 / v4.1-Flash / GLM-5.2
    m = re.search(r"\b([Vv]\d+(?:\.\d+)*(?:-[\w]+)?)\b", text)
    if m:
        return m.group(1)
    return None


def parse_page(html: str, row: dict | None = None) -> dict:
    """从厂商页面 HTML 提取四个监控字段。

    返回 {field: value|None}，value 为 str 或 float；任何字段提取不到就是 None。
    不做猜测。
    """
    text = _strip_html(html)
    price_in, in_unit = _extract_price(text, "in")
    price_out, out_unit = _extract_price(text, "out")
    return {
        "version": _extract_version(text),
        "price_in": price_in,
        "price_out": price_out,
        "context_window": _extract_context_window(text),
        "unit_notes": {
            "price_in": in_unit,
            "price_out": out_unit,
        },
    }


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------
def _normalize(v) -> float | str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    s = str(v).strip()
    if s == "":
        return None
    try:
        return round(float(s), 4)
    except ValueError:
        return s


def diff_model(old_row: dict, extracted: dict) -> dict:
    """对比 CSV 旧值与页面新值。返回 {field: {old, new, status, writable, note}}。"""
    out = {}
    unit_notes = extracted.get("unit_notes", {}) or {}
    for field in MONITORED_FIELDS:
        old = (old_row.get(field) or "").strip()
        new = extracted.get(field)
        note = unit_notes.get(field) if field in ("price_in", "price_out") else None
        writable = True
        if new is None:
            out[field] = {"old": old, "new": None, "status": STATUS_PENDING,
                          "writable": False, "note": note}
            continue
        old_norm = _normalize(old)
        new_norm = _normalize(new)
        if old_norm is None:
            # CSV 原本是 待补充/待核实/空 → 抓到了就算补全，记为有变更
            status = STATUS_CHANGED
        elif old_norm == new_norm:
            status = STATUS_NOCHANGE
        else:
            status = STATUS_CHANGED
        # 单位不对齐的不写入 .new
        if note:
            writable = False
        out[field] = {"old": old, "new": new, "status": status,
                      "writable": writable, "note": note}
    return out


def overall_status(field_diffs: dict, fetch_ok: bool) -> str:
    if not fetch_ok:
        return STATUS_FETCH_FAIL
    statuses = {d["status"] for d in field_diffs.values()}
    if STATUS_CHANGED in statuses:
        return STATUS_CHANGED
    if statuses == {STATUS_PENDING}:
        return STATUS_PENDING
    return STATUS_NOCHANGE


# ---------------------------------------------------------------------------
# 单模型处理
# ---------------------------------------------------------------------------
def process_model(row: dict, dry_run: bool = False) -> dict:
    url = first_source_url(row)
    name = row.get("logical_name", "?")
    record = {
        "logical_name": name,
        "vendor": row.get("vendor", ""),
        "source_url": url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "http_status": None,
        "status": STATUS_FETCH_FAIL,
        "error": None,
        "fields": {},
    }
    if not url:
        record["error"] = "无 source_url"
        return record
    res = fetch_url(url, dry_run=dry_run)
    record["http_status"] = res["status_code"]
    if not res["ok"]:
        record["error"] = res["error"]
        record["status"] = STATUS_FETCH_FAIL
        return record
    record["error"] = None
    if dry_run:
        record["status"] = STATUS_REACHABLE
        return record
    extracted = parse_page(res["html"] or "", row)
    field_diffs = diff_model(row, extracted)
    record["fields"] = field_diffs
    record["status"] = overall_status(field_diffs, fetch_ok=True)
    return record


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------
def generate_report(results: list[dict], output_path: Path) -> dict:
    summary = {STATUS_NOCHANGE: 0, STATUS_CHANGED: 0,
               STATUS_FETCH_FAIL: 0, STATUS_PENDING: 0, STATUS_REACHABLE: 0}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "summary": summary,
        "models": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def write_updated_csv(rows: list[dict], results: list[dict], new_path: Path) -> None:
    """生成 data/models.csv.new：把可写入的变更落进去，as_of 更新为当天。"""
    res_by_name = {r["logical_name"]: r for r in results}
    today = datetime.now().strftime("%Y-%m-%d")
    fieldnames = list(rows[0].keys())
    with open(new_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            new_row = dict(row)
            res = res_by_name.get(row["logical_name"], {})
            for field, info in (res.get("fields") or {}).items():
                if info.get("status") == STATUS_CHANGED and info.get("writable") and info.get("new") is not None:
                    new_row[field] = str(info["new"])
            new_row["as_of"] = today
            w.writerow(new_row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="模型版本/价格监控（ADR-0008）")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="models.csv 路径")
    ap.add_argument("--output", default=None, help="JSON 报告输出路径")
    ap.add_argument("--dry-run", action="store_true", help="只校验 URL 可达性，不解析页面")
    ap.add_argument("--update", action="store_true",
                    help="生成 data/models.csv.new（不覆盖原文件）")
    args = ap.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[错误] 找不到 {csv_path}", file=sys.stderr)
        return 1

    rows = load_models(csv_path)
    mode = "dry-run" if args.dry_run else "monitor"
    print(f"=== 模型版本监控（{mode}）：{len(rows)} 个模型 ===")

    results = []
    for row in rows:
        rec = process_model(row, dry_run=args.dry_run)
        results.append(rec)
        # 终端摘要
        extra = ""
        if rec["status"] == STATUS_CHANGED:
            changed = [f for f, d in rec["fields"].items() if d["status"] == STATUS_CHANGED]
            extra = f" 变更字段: {','.join(changed)}"
        elif rec["status"] == STATUS_FETCH_FAIL and rec.get("error"):
            extra = f" ({rec['error'][:80]})"
        print(f"  - {rec['logical_name']:<28} [{rec['status']}]{extra}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path(args.output) if args.output else (DEFAULT_REPORTS_DIR / f"version-monitor-{ts}.json")
    report = generate_report(results, output)

    print(f"\n摘要: 无变化={report['summary'].get(STATUS_NOCHANGE,0)}  "
          f"有变更={report['summary'].get(STATUS_CHANGED,0)}  "
          f"待补充={report['summary'].get(STATUS_PENDING,0)}  "
          f"抓取失败={report['summary'].get(STATUS_FETCH_FAIL,0)}  "
          f"可达={report['summary'].get(STATUS_REACHABLE,0)}")
    print(f"报告: {output}")

    if args.update and not args.dry_run:
        new_csv = csv_path.with_suffix(".csv.new")
        write_updated_csv(rows, results, new_csv)
        n_changed = sum(1 for r in results if r["status"] == STATUS_CHANGED)
        print(f"已生成 {new_csv}（{n_changed} 个模型有变更待人工 diff 确认；未覆盖原文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
