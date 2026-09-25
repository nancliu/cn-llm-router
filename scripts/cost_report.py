#!/usr/bin/env python3
"""用量与成本月度账单（ADR-0011）。

读取 JSONL 用量日志（默认 reports/usage.jsonl），按月份汇总：
- 总调用次数 / 总估算成本（元）
- 按模型分组（次数 / 成本 / 占比）
- 按类别分组
- 按事件类型分组（classify / route / completion）

成本数字直接来自日志条目的 estimated_cost_yuan（与 ADR-0001 口径一致），
不做二次估算；缺价条目成本记 None，汇总时计入"未知成本"。

用法:
    python scripts/cost_report.py [--log reports/usage.jsonl] [--month 2026-09] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_events(path: str) -> list[dict]:
    """读取 JSONL 日志；文件不存在返回空列表。坏行跳过（不中断汇总）。"""
    if not os.path.exists(path):
        return []
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def filter_month(events: list[dict], month: str | None) -> list[dict]:
    """按 YYYY-MM 过滤；month 为 None 时用当月。按 timestamp 前缀匹配。"""
    if month is None:
        month = datetime.now().strftime("%Y-%m")
    out = []
    for e in events:
        ts = str(e.get("timestamp", ""))
        if ts[:7] == month:
            out.append(e)
    return out


def _cost_of(e: dict) -> float:
    c = e.get("estimated_cost_yuan")
    return float(c) if isinstance(c, (int, float)) else 0.0


def summarize(events: list[dict]) -> dict:
    """汇总一组事件。所有数字直接来自日志条目，可溯源。"""
    total = len(events)
    total_cost = sum(_cost_of(e) for e in events)
    unknown_cost_n = sum(1 for e in events if e.get("estimated_cost_yuan") is None)

    def _group(key: str) -> dict:
        cnt: dict[str, int] = defaultdict(int)
        cost: dict[str, float] = defaultdict(float)
        for e in events:
            k = e.get(key) or "(空)"
            cnt[k] += 1
            cost[k] += _cost_of(e)
        rows = [
            {"name": k, "count": cnt[k], "cost_yuan": round(cost[k], 6),
             "count_share": round(cnt[k] / total, 4) if total else 0.0,
             "cost_share": round(cost[k] / total_cost, 4) if total_cost else 0.0}
            for k in sorted(cnt, key=lambda x: -cost[x])
        ]
        return rows

    return {
        "total_events": total,
        "total_cost_yuan": round(total_cost, 6),
        "unknown_cost_events": unknown_cost_n,
        "by_event_type": _group("event_type"),
        "by_model": _group("primary_model"),
        "by_category": _group("category"),
    }


def print_report(s: dict, month: str) -> None:
    print(f"\n== 用量成本月报 {month} ==")
    print(f"  事件总数: {s['total_events']}")
    print(f"  总成本（估算/实际，元）: {s['total_cost_yuan']:.4f}")
    if s["unknown_cost_events"]:
        print(f"  缺价条目（成本未知）: {s['unknown_cost_events']}")

    def _table(title: str, rows: list[dict]) -> None:
        print(f"\n  [{title}]")
        if not rows:
            print("    （无）")
            return
        for r in rows:
            print(f"    {r['name']:<24} 次数={r['count']:<5} 成本={r['cost_yuan']:.4f}元 "
                  f"({r['cost_share']:.1%})")

    _table("按事件类型", s["by_event_type"])
    _table("按模型", s["by_model"])
    _table("按类别", s["by_category"])


def main() -> None:
    ap = argparse.ArgumentParser(description="用量与成本月度账单（ADR-0011）")
    default_log = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "usage.jsonl"
    )
    ap.add_argument("--log", default=default_log, help=f"JSONL 日志路径（默认 {default_log}）")
    ap.add_argument("--month", default=None, help="月份 YYYY-MM（缺省当月）")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args()

    events = load_events(args.log)
    if not events:
        print(f"未找到日志文件或为空：{args.log}")
        print("（开启统计：config/selector.yaml 设 stats_enabled=true，见 ADR-0011）")
        return

    month = args.month or datetime.now().strftime("%Y-%m")
    selected = filter_month(events, month)
    summary = summarize(selected)

    if args.json:
        print(json.dumps({"month": month, **summary}, ensure_ascii=False, indent=2))
    else:
        print_report(summary, month)


if __name__ == "__main__":
    main()
