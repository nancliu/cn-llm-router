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
        [--check-baseline]   # Release 门禁（ADR-0013）：对比基线阈值，低于则 exit 1
    python3 scripts/eval_classifier.py --list-ready   # 只看哪些模型已配/未配 key，不调用 LLM

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


# 评测门禁基线（ADR-0013）。数字必须可溯源：
# 来源 reports/eval-20260924.json，2026-09-24 全量 180 题人工评测；
# 发布前重跑评测后回写本常量，禁止凭空编造。
EVAL_BASELINE = {
    "Qwen3.8-Max-0902": 0.994,
    "DeepSeek-V4.1-Flash-CED": 0.989,
    "豆包Seed-2.1-Pro": 0.989,
}
EVAL_THRESHOLD = 0.97  # 端到端准确率绝对下限，低于此值拦截发布


def check_baseline(
    results: list[dict],
    available: set[str],
    baseline: dict[str, float] = EVAL_BASELINE,
    threshold: float = EVAL_THRESHOLD,
) -> tuple[bool, list[str]]:
    """对比已跑模型的端到端准确率与门禁阈值（纯函数，可单测）。

    results: run_model/summarize 的输出列表（含 end_to_end_accuracy）。
    available: 本次已配置 API key、实际参与评测的模型名集合。
    返回 (ok, messages)：ok=False 时调用方应 sys.exit(1)。
    """
    by_model = {r["model"]: r for r in results}
    messages: list[str] = []
    ok = True
    for model, base in baseline.items():
        if model not in available:
            messages.append(f"未配置 key，跳过：{model}（基线 {base:.1%}）")
            continue
        res = by_model.get(model)
        if res is None:
            messages.append(f"{model}：已配 key 但未产出评测结果，按失败处理")
            ok = False
            continue
        acc = res["end_to_end_accuracy"]
        if acc < threshold:
            ok = False
            messages.append(
                f"{model}：端到端准确率 {acc:.1%} 低于门禁阈值 {threshold:.0%}"
                f"（基线 {base:.1%}）——拦截发布"
            )
        else:
            messages.append(
                f"{model}：端到端准确率 {acc:.1%}（基线 {base:.1%}，阈值 {threshold:.0%}）通过"
            )
    return ok, messages


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


def list_ready(cfg) -> None:
    """打印当前已配 / 未配 key 的模型清单（不加载 golden cases、不调用任何 LLM）。

    覆盖 config/providers.yaml（缺省 providers.example.yaml）里的全部模型，
    未配 key 的一并打出 key_env 变量名，方便照 .env.example 补全。
    """
    ready, waiting = [], []
    for name, spec in cfg.providers.items():
        if key_available(spec):
            ready.append((name, spec.provider, spec.key_env))
        else:
            waiting.append((name, spec.provider, spec.key_env))
    print(f"== key 就绪清单（共 {len(cfg.providers)} 个模型）==")
    print(f"\n已配置 key（{len(ready)} 个，可直接参与评测）：")
    for name, prov, env in ready:
        print(f"  [就绪] {name:<26} provider={prov:<12} {env}=***")
    print(f"\n未配置 key（{len(waiting)} 个，评测时自动跳过）：")
    for name, prov, env in waiting:
        print(f"  [待配] {name:<26} provider={prov:<12} 需环境变量 {env}")
    print("\nkey 获取入口与 .env 模板见 docs/eval-setup.md；"
          "配好后重跑本脚本（或 --dry-run 先校验）即可纳入。")


def main():
    ap = argparse.ArgumentParser(description="LLM 判类在线评测")
    ap.add_argument("--models", default=None, help="候选模型逗号分隔；缺省读 config/classifier.example.yaml 的 models")
    ap.add_argument("--cases", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "tests", "fixtures", "golden_cases.yaml"))
    ap.add_argument("--output", default=None, help="JSON 报告落盘路径（缺省不落盘）")
    ap.add_argument("--dry-run", action="store_true", help="不调用 LLM：校验 key 状态与数据结构")
    ap.add_argument("--check-baseline", action="store_true",
                    help="Release 门禁：对比 EVAL_BASELINE 阈值，低于 EVAL_THRESHOLD 则 exit 1")
    ap.add_argument("--list-ready", action="store_true",
                    help="只打印已配/未配 key 的模型清单（含 key_env 变量名），不加载 cases、不调用 LLM")
    args = ap.parse_args()

    cfg = load_config()

    if args.list_ready:
        list_ready(cfg)
        return

    data = load_data(cfg.data_dir)
    cases = load_cases(args.cases)

    models = args.models.split(",") if args.models else list(cfg.classifier_models)
    ready = []
    # 明确打印每个未配 key / 未注册模型的跳过原因（不再静默排除）
    for m in models:
        spec = cfg.providers.get(m)
        if spec is None:
            print(f"未在 config/providers.yaml 注册，跳过 {m}")
        elif not key_available(spec):
            print(f"未配置 {spec.key_env}，跳过 {m}")
        else:
            ready.append(m)
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

    if args.check_baseline:
        ok, messages = check_baseline(results, set(ready))
        print("\n== 基线门禁检查（ADR-0013） ==")
        for m in messages:
            print("  " + m)
        if not ok:
            print("门禁未通过，exit 1。")
            sys.exit(1)
        print("门禁通过。")


if __name__ == "__main__":
    main()
