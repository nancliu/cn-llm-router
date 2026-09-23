#!/usr/bin/env python3
"""在线评测 LLM 判类准确率（README backlog 第 2 项）。

逐个候选分类模型跑全部 golden cases（108 题，12 类 × 3 复杂度 × 3 题），统计：
- 端到端准确率：classify() 最终类别（含兜底）命中期望类别
- LLM 直判准确率：debug=True 下 LLM 返回合法 JSON 且类别命中（未走兜底）
- 复杂度命中率 / top-2 命中率（second_guess）/ 12×12 混淆矩阵
- 推荐默认分类模型：端到端准确率最高者

用法:
    python3 scripts/eval_classifier.py \
        [--models GLM-5.3-Flash,DeepSeek-V4-Flash-0731] \
        [--cases tests/fixtures/golden_cases.yaml] \
        [--output reports/eval-<ts>.json] \
        [--dry-run]          # 不调用任何 LLM：只校验 key 状态与数据结构

前置：至少一个候选模型已配置 API key（providers.example.yaml 的 key_env 对应环境变量）。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cn_llm_router.config import load_config, key_available  # noqa: E402
from cn_llm_router.data_loader import load_data  # noqa: E402


def load_cases(path: str) -> list[dict]:
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    cases = doc.get("cases") or []
    assert cases, f"无 golden cases: {path}"
    # 校验结构
    cats = set()
    for c in cases:
        assert c.get("id") and c.get("prompt") and c.get("category"), c
        cats.add(c["category"])
    return cases


def build_classifier(cfg, data, model: str, debug=True):
    """固定候选链为该模型，返回 Classifier 实例。"""
    import dataclasses
    from cn_llm_router.classifier import Classifier

    c = dataclasses.replace(cfg, classifier_models=[model])
    return Classifier(data=data, cfg=c, debug=debug)


def run_model(cfg, data, model: str, cases: list[dict]) -> dict:
    clf = build_classifier(cfg, data, model)
    rows, t0 = [], time.time()
    for case in cases:
        res = clf.classify(case["prompt"])
        rows.append({
            "id": case["id"],
            "prompt": case["prompt"],
            "expected": case["category"],
            "expected_complexity": case.get("complexity"),
            "actual": res.category,
            "actual_complexity": res.complexity,
            "confidence": res.confidence,
            "second_guess": res.second_guess,
            "llm_direct": res.raw is not None and res.raw.get("category") == case["category"],
            "llm_raw_category": (res.raw or {}).get("category"),
            "fallback_reason": res.fallback_reason,
        })
    elapsed = time.time() - t0
    return summarize(model, rows, elapsed)


def summarize(model: str, rows: list[dict], elapsed: float) -> dict:
    n = len(rows)
    e2e_hit = sum(1 for r in rows if r["actual"] == r["expected"])
    llm_direct = sum(1 for r in rows if r["llm_direct"])
    llm_total = sum(1 for r in rows if r["llm_raw_category"] is not None)
    cx_cases = [r for r in rows if r["expected_complexity"]]
    cx_hit = sum(1 for r in cx_cases if r["actual_complexity"] == r["expected_complexity"])
    top2_hit = sum(
        1 for r in rows
        if r["second_guess"] and (r["second_guess"] == r["expected"] or r["actual"] == r["expected"])
    )
    # 按类别 / 复杂度准确率
    by_cat, by_cx = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for r in rows:
        by_cat[r["expected"]][1] += 1
        by_cat[r["expected"]][0] += 1 if r["actual"] == r["expected"] else 0
        by_cx[r["expected_complexity"] or "—"][1] += 1
        by_cx[r["expected_complexity"] or "—"][0] += 1 if r["actual"] == r["expected"] else 0
    # 混淆矩阵（实际类别 × 期望类别）
    cats = sorted({r["expected"] for r in rows} | {r["actual"] for r in rows})
    conf = {c: {x: 0 for x in cats} for c in cats}
    for r in rows:
        conf[r["actual"]][r["expected"]] += 1
    return {
        "model": model,
        "n": n,
        "elapsed_s": round(elapsed, 1),
        "end_to_end_accuracy": round(e2e_hit / n, 4),
        "llm_direct_accuracy": round(llm_direct / llm_total, 4) if llm_total else None,
        "llm_direct_cases": llm_total,
        "complexity_accuracy": round(cx_hit / len(cx_cases), 4) if cx_cases else None,
        "top2_hit_rate": round(top2_hit / n, 4),
        "by_category": {k: f"{h}/{t}" for k, (h, t) in sorted(by_cat.items())},
        "by_complexity": {k: f"{h}/{t}" for k, (h, t) in sorted(by_cx.items())},
        "confusion": conf,
        "rows": rows,
    }


def print_report(res: dict) -> None:
    print(f"\n== {res['model']} ==")
    print(f"  端到端准确率: {res['end_to_end_accuracy']:.1%}  ({res['n']} 题, {res['elapsed_s']}s)")
    if res["llm_direct_accuracy"] is not None:
        print(f"  LLM 直判准确率: {res['llm_direct_accuracy']:.1%}  (LLM 直判 {res['llm_direct_cases']}/{res['n']} 题)")
    if res["complexity_accuracy"] is not None:
        print(f"  复杂度命中率: {res['complexity_accuracy']:.1%}    top-2 命中率: {res['top2_hit_rate']:.1%}")
    print("  按类别:")
    for k, v in res["by_category"].items():
        print(f"    {k}: {v}")
    print("  按复杂度:", ", ".join(f"{k}={v}" for k, v in res["by_complexity"].items()))
    errs = [r for r in res["rows"] if r["actual"] != r["expected"]]
    if errs:
        print(f"  错判 {len(errs)} 题:")
        for r in errs[:12]:
            hint = f"（LLM判为 {r['llm_raw_category']}）" if r["llm_raw_category"] else f"（{r['fallback_reason']}）"
            print(f"    [{r['id']}] 期望={r['expected']} 实际={r['actual']} {hint}")
            if len([x for x in errs if x["actual"] != x["expected"]]) > 12 and r is errs[11]:
                print("    ...（其余省略，见 JSON 报告）")


def main():
    ap = argparse.ArgumentParser(description="LLM 判类在线评测")
    ap.add_argument("--models", default=None, help="候选模型逗号分隔；缺省读 config/classifier.example.yaml 的 models")
    ap.add_argument("--cases", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "tests", "fixtures", "golden_cases.yaml"))
    ap.add_argument("--output", default=None, help="JSON 报告落盘路径（缺省不落盘）")
    ap.add_argument("--dry-run", action="store_true", help="不调用 LLM：校验 key 状态与数据结构")
    args = ap.parse_args()

    cfg = load_config()
    data = load_data(cfg.data_dir)
    cases = load_cases(args.cases)

    models = args.models.split(",") if args.models else list(cfg.classifier_models)
    ready = [m for m in models if key_available(cfg.providers.get(m))]
    if not ready:
        need = ", ".join(f"{m}({cfg.providers[m].key_env})" for m in models if m in cfg.providers)
        print(f"未配置任何可用 API key：{need}")
        print("评测需至少一个候选分类模型的 key（如 ZHIPU_API_KEY=...）。")
        print("--dry-run 模式已退出（未调用任何 LLM）。")
        sys.exit(2 if not args.dry_run else 0)
    print(f"可用模型: {ready}")
    print(f"golden cases: {len(cases)} 题（{len({c['category'] for c in cases})} 类 × "
          f"{len({c['complexity'] for c in cases})} 复杂度 × 3）")

    if args.dry_run:
        print("dry-run：结构与 key 检查通过，未调用 LLM。")
        return

    results = [run_model(cfg, data, m, cases) for m in ready]
    results.sort(key=lambda r: r["end_to_end_accuracy"], reverse=True)
    for r in results:
        print_report(r)
    best = results[0]
    default = cfg.classifier_models[0]
    print(f"\n推荐默认分类模型: {best['model']}（端到端 {best['end_to_end_accuracy']:.1%}）"
          + ("" if best["model"] == default else f"，当前默认 {default}，可更新 config/classifier.example.yaml"))
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"JSON 报告 -> {args.output}")


if __name__ == "__main__":
    main()
