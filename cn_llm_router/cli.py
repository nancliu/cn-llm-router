"""命令行入口（README backlog 第 3 项）。

子命令:
    classify <prompt>          # 任务分类（LLM 判类，未配 key 走兜底）
    select --category <名> --complexity <低|中|高>   # 模型推荐
    route <prompt>             # 分类 + 推荐 + 就绪的 OpenAI 兼容客户端
    list-models                # 模型注册表
    list-categories            # 12 类定义
    list-strategies            # 三档策略说明

所有命令支持 --json 输出（stdout 仅一份 JSON）。
"""
import argparse
import dataclasses
import json
import sys

from .config import load_config
from .data_loader import load_data


def _json_dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _model_summary(m):
    d = dataclasses.asdict(m)
    return {
        "logical_name": d["logical_name"],
        "vendor": d["vendor"],
        "version": d["version"],
        "context_window": d["context_window"],
        "price_in": d["price_in"],
        "price_out": d["price_out"],
        "open_source": d["open_source"],
    }


def cmd_classify(args, cfg, data):
    from .classifier import Classifier

    clf = Classifier(data=data, cfg=cfg, debug=args.debug).classify(args.prompt)
    out = {
        "category": clf.category,
        "complexity": clf.complexity,
        "confidence": clf.confidence,
        "second_guess": clf.second_guess,
        "low_confidence": clf.low_confidence,
        "fallback_reason": clf.fallback_reason,
        "raw": clf.raw,
    }
    if args.json:
        _json_dump(out)
    else:
        tag = "（low_confidence）" if clf.low_confidence else ""
        reason = f"，{clf.fallback_reason}" if clf.fallback_reason else ""
        print(f"类别: {clf.category}  复杂度: {clf.complexity}  置信度: {clf.confidence:.2f}{tag}{reason}")
        if clf.second_guess:
            print(f"次选: {clf.second_guess}")


def cmd_select(args, cfg, data):
    from .selector import select as _select

    rec = _select(data, args.category, args.complexity, strategy=args.strategy,
                  availability_filter=False if args.no_availability_filter else None, cfg=cfg)
    if args.json:
        _json_dump(_rec_to_dict(rec))
        return
    print(f"[{args.strategy}] {args.category} / {args.complexity}")
    print(f"主选: {rec.primary.logical_name}  厂商: {rec.primary.vendor}  能力分: {rec.primary.score}  成本: {rec.primary.cost}元/百万tok")
    print(f"备选: {rec.backup.logical_name}  厂商: {rec.backup.vendor}  能力分: {rec.backup.score}  成本: {rec.backup.cost}元/百万tok")
    for n in rec.notice:
        print(f"提示: {n}")


def cmd_route(args, cfg, data):
    from .classifier import Classifier
    from .gateway import RouterClient
    from .selector import select as _select

    clf = Classifier(data=data, cfg=cfg, debug=args.debug).classify(args.prompt)
    rec = _select(data, clf.category, clf.complexity, strategy=args.strategy,
                  availability_filter=False if args.no_availability_filter else None, cfg=cfg)
    client = RouterClient(rec, cfg=cfg)
    out = {
        "request": args.prompt,
        "classification": {
            "category": clf.category, "complexity": clf.complexity,
            "confidence": clf.confidence, "low_confidence": clf.low_confidence,
            "fallback_reason": clf.fallback_reason,
        },
        "recommendation": _rec_to_dict(rec),
        "client": {
            "type": type(client).__name__,
            "ready": True,
            "usage_hint": "client.chat.completions.create(model=<logical_name>, messages=[...])",
        },
    }
    if args.json:
        _json_dump(out)
    else:
        print(f"分类: {clf.category} / {clf.complexity}（置信 {clf.confidence:.2f}）")
        print(f"主选: {rec.primary.logical_name}  备选: {rec.backup.logical_name}")
        for n in rec.notice:
            print(f"提示: {n}")
        print(f"客户端就绪: {type(client).__name__}（OpenAI 兼容，失败自动切备选）")


def _rec_to_dict(rec):
    return {
        "strategy": rec.strategy,
        "availability_filtered": rec.availability_filtered,
        "primary": {
            "logical_name": rec.primary.logical_name, "vendor": rec.primary.vendor,
            "score": rec.primary.score, "cost": rec.primary.cost, "reason": rec.primary.reason,
        },
        "backup": (
            {"logical_name": rec.backup.logical_name, "vendor": rec.backup.vendor,
             "score": rec.backup.score, "cost": rec.backup.cost, "reason": rec.backup.reason}
            if rec.backup else None
        ),
        "notice": rec.notice,
    }


def cmd_list_models(args, cfg, data):
    rows = [_model_summary(m) for m in data.models.values()]
    if args.json:
        _json_dump(rows)
        return
    print(f"{'逻辑模型名':<26}{'厂商':<14}{'版本':<18}{'上下文':<12}{'输入价':>8}{'输出价':>8}")
    for m in rows:
        pin = f"{m['price_in']:.2f}" if m["price_in"] is not None else "—"
        pout = f"{m['price_out']:.2f}" if m["price_out"] is not None else "—"
        print(f"{m['logical_name']:<26}{m['vendor']:<14}{m['version']:<18}{m['context_window']:<12}{pin:>8}{pout:>8}")


def cmd_list_categories(args, cfg, data):
    rows = [{"id": c.id, "name": c.name, "description": c.description} for c in data.categories.values()]
    if args.json:
        _json_dump(rows)
        return
    for r in rows:
        print(f"{r['id']:<20}{r['name']} — {r['description']}")


def cmd_list_strategies(args, cfg, data):
    rows = [
        {"strategy": "纯能力优先", "rule": "成本权重=0，仅按能力分降序，同分取成本低者"},
        {"strategy": "平衡", "rule": "低复杂度偏性价比、中复杂度平衡、高复杂度偏能力（默认）"},
        {"strategy": "性价比优先", "rule": "按 能力分/成本指数 排序，能力分<50 设门槛"},
    ]
    if args.json:
        _json_dump(rows)
        return
    for r in rows:
        print(f"{r['strategy']}: {r['rule']}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cn-llm-router", description="国内大模型选择器/路由器")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--debug", action="store_true", help="分类器 debug（raw 输出）")
    ap.add_argument("--config-dir", default=None, help="配置目录（缺省 config/ 或 CN_LLM_ROUTER_CONFIG）")
    # 全局选项同时挂到每个子命令（支持 --json 放在子命令前或后）
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", help="JSON 输出")
    parent.add_argument("--debug", action="store_true", help="分类器 debug（raw 输出）")
    parent.add_argument("--config-dir", default=None, help="配置目录（缺省 config/ 或 CN_LLM_ROUTER_CONFIG）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("classify", parents=[parent], help="任务分类")
    p.add_argument("prompt")

    p = sub.add_parser("select", parents=[parent], help="模型推荐")
    p.add_argument("--category", required=True, help="12 类之一（cn-llm-router list-categories 查看）")
    p.add_argument("--complexity", required=True, choices=["低", "中", "高"])
    p.add_argument("--strategy", default="平衡", choices=["纯能力优先", "平衡", "性价比优先"])
    p.add_argument("--no-availability-filter", action="store_true", help="关闭可用性过滤（从全量集比较）")

    p = sub.add_parser("route", parents=[parent], help="分类+推荐+就绪客户端")
    p.add_argument("prompt")
    p.add_argument("--strategy", default="平衡", choices=["纯能力优先", "平衡", "性价比优先"])
    p.add_argument("--no-availability-filter", action="store_true")

    sub.add_parser("list-models", parents=[parent], help="模型注册表")
    sub.add_parser("list-categories", parents=[parent], help="12 类定义")
    sub.add_parser("list-strategies", parents=[parent], help="三档策略说明")

    args = ap.parse_args(argv)
    cfg = load_config(args.config_dir)
    data = load_data(cfg.data_dir)

    handlers = {
        "classify": cmd_classify, "select": cmd_select, "route": cmd_route,
        "list-models": cmd_list_models, "list-categories": cmd_list_categories,
        "list-strategies": cmd_list_strategies,
    }
    try:
        handlers[args.cmd](args, cfg, data)
    except (ValueError, KeyError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
