#!/usr/bin/env python3
"""手动评测统计：用"当前模型"（MainAgent 直判）替代厂商 API 跑 golden cases（README 工具链补充）。

流程:
    1. 人类/Agent 逐题给出 judgment: {"id": ..., "category": ..., "complexity": ...,
       "confidence": 0.x, "second_guess": ...}
    2. python3 scripts/eval_manual.py <judgments.json> [--cases tests/fixtures/golden_cases.yaml]
       → 复用 eval_classifier.summarize 输出端到端/直判准确率、按类别矩阵、混淆矩阵、错判清单

judgments.json 结构:
    {"model": "名称", "judgments": [{"id": "prog-low", "category": "程序编码", "complexity": "低",
                                     "confidence": 0.9, "second_guess": null}]}
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_classifier import load_cases, print_report, summarize  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="手动评测统计（当前模型直判替代厂商 API）")
    ap.add_argument("judgments", help="judgments JSON 文件")
    ap.add_argument("--cases", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "tests", "fixtures", "golden_cases.yaml"))
    ap.add_argument("--output", default=None, help="报告 JSON 落盘路径（缺省不落盘）")
    args = ap.parse_args()

    with open(args.judgments, encoding="utf-8") as f:
        doc = json.load(f)
    model = doc.get("model", "manual")
    judgments = {j["id"]: j for j in doc["judgments"]}

    cases = load_cases(args.cases)
    missing = [c["id"] for c in cases if c["id"] not in judgments]
    if missing:
        sys.exit(f"judgments 缺少 {len(missing)} 题: {missing[:10]} ...")

    rows = []
    for c in cases:
        j = judgments[c["id"]]
        actual = j["category"]
        rows.append({
            "id": c["id"],
            "prompt": c["prompt"],
            "expected": c["category"],
            "expected_complexity": c.get("complexity"),
            "actual": actual,
            "actual_complexity": j.get("complexity"),
            "confidence": j.get("confidence"),
            "second_guess": j.get("second_guess"),
            "llm_direct": actual == c["category"],  # 手动判断即"直判"
            "llm_raw_category": actual,
            "fallback_reason": None,
        })

    res = summarize(model, rows, 0.0)
    print_report(res)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"JSON 报告 -> {args.output}")


if __name__ == "__main__":
    main()
