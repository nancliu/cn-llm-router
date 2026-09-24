#!/usr/bin/env python3
"""多模态（VLM）真实实测脚本（P2-b）。

从 golden_cases.yaml 筛 category="多模态理解" 的题目，配上本地生成的纯色测试 PNG，
调用支持 VLM 的模型（OpenAI 兼容多模态 messages 格式），用关键词命中判定答案正确性。

v1 说明：
- 测试图为纯 Python 标准库（struct/zlib）生成的纯色 PNG，不依赖 PIL；
  仅验证"模型是否真的读到了图片"（基础颜色识别）。OCR/场景理解等更复杂的多模态
  评测留作 backlog（需要带文字/真实物体的图片素材）。
- 评分：expected_keywords 任一命中（大小写不敏感）即算正确。

用法:
    python scripts/eval_multimodal.py [--models GLM-5.3-Flash,Qwen3.8-Max-0902] \
        [--output reports/mm-eval-<ts>.json] [--dry-run]
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cn_llm_router.config import load_config, key_available  # noqa: E402

# 测试图目录与配色（rgb, expected_keywords）
IMG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tests", "fixtures", "mm_images")
PALETTE = [
    ("red",    (220, 40, 40),  ["红色", "红", "red"]),
    ("green",  (40, 160, 80),  ["绿色", "绿", "green"]),
    ("blue",   (40, 80, 220),  ["蓝色", "蓝", "blue"]),
    ("yellow", (230, 200, 50), ["黄色", "黄", "yellow"]),
]
DEFAULT_VLM_MODELS = ["GLM-5.3-Flash", "Qwen3.8-Max-0902", "DeepSeek-V4.1-Flash-CED"]


def make_png(path: str, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    """用标准库写一张纯色 PNG（无 PIL 依赖）。"""
    def chunk(typ: bytes, data: bytes) -> bytes:
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width  # 每行前置 filter byte=0
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit 真彩色
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(raw)))
        f.write(chunk(b"IEND", b""))


def ensure_images() -> dict[str, str]:
    """确保测试图存在，返回 {color_name: abs_png_path}。"""
    os.makedirs(IMG_DIR, exist_ok=True)
    out = {}
    for name, rgb, _kw in PALETTE:
        p = os.path.join(IMG_DIR, f"{name}.png")
        if not os.path.exists(p):
            make_png(p, 128, 128, rgb)
        out[name] = p
    return out


def load_mm_cases(path: str) -> list[dict]:
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    cases = [c for c in (doc.get("cases") or []) if c.get("category") == "多模态理解"]
    assert cases, f"golden_cases 中无多模态理解题目: {path}"
    return cases


def build_scenarios(cases: list[dict], images: dict[str, str]) -> list[dict]:
    """把每个多模态题目配一张测试图 + 颜色识别问题 + 期望关键词。

    v1 统一问"主色调"：模型若没真正读图，无法猜中颜色（文本模型必错），
    以此验证 VLM 的图像输入链路是否生效。
    """
    palette_names = [p[0] for p in PALETTE]
    kw_map = {p[0]: p[2] for p in PALETTE}
    scenarios = []
    for i, case in enumerate(cases):
        color = palette_names[i % len(palette_names)]
        img_path = images[color]
        question = (
            f"{case['prompt']}\n\n"
            "现在请根据这张实际图片回答：它的主色调是什么颜色？只需说出颜色名称。"
        )
        scenarios.append({
            "case_id": case["id"],
            "complexity": case.get("complexity"),
            "prompt_text": question,
            "image": img_path,
            "color": color,
            "expected_keywords": kw_map[color],
        })
    return scenarios


def img_to_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def ask_vlm(cfg, model: str, scenario: dict) -> str:
    """调用 VLM：messages content 用 list 形式（text + image_url）。"""
    from cn_llm_router.gateway import build_client

    prov = cfg.providers[model]
    client = build_client(prov)
    resp = client.chat.completions.create(
        model=prov.api_model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": scenario["prompt_text"]},
                {"type": "image_url", "image_url": {"url": img_to_data_uri(scenario["image"])}},
            ],
        }],
        temperature=0,
        max_tokens=128,
        timeout=prov.timeout,
    )
    return (resp.choices[0].message.content or "").strip()


def score(answer: str, keywords: list[str]) -> bool:
    low = answer.lower()
    return any(kw.lower() in low for kw in keywords)


def run_model(cfg, model: str, scenarios: list[dict]) -> dict:
    rows, t0 = [], time.time()
    for sc in scenarios:
        try:
            ans = ask_vlm(cfg, model, sc)
            hit = score(ans, sc["expected_keywords"])
            err = None
        except Exception as e:  # noqa: BLE001 —— 单题失败不中断整模型
            ans, hit, err = "", False, f"{type(e).__name__}: {e}"
        rows.append({
            "case_id": sc["case_id"],
            "complexity": sc["complexity"],
            "image": os.path.basename(scenario_image(sc)),
            "expected_color": sc["color"],
            "expected_keywords": sc["expected_keywords"],
            "answer": ans,
            "hit": hit,
            "error": err,
        })
    elapsed = time.time() - t0
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    by_cx = defaultdict(lambda: [0, 0])
    for r in rows:
        by_cx[r["complexity"] or "—"][1] += 1
        by_cx[r["complexity"] or "—"][0] += 1 if r["hit"] else 0
    return {
        "model": model,
        "n": n,
        "elapsed_s": round(elapsed, 1),
        "accuracy": round(hits / n, 4) if n else None,
        "by_complexity": {k: f"{h}/{t}" for k, (h, t) in sorted(by_cx.items())},
        "rows": rows,
    }


def scenario_image(sc: dict) -> str:
    return sc["image"]


def print_report(res: dict) -> None:
    print(f"\n== {res['model']} ==")
    print(f"  准确率: {res['accuracy']:.1%}  ({res['n']} 题, {res['elapsed_s']}s)")
    print("  按复杂度:", ", ".join(f"{k}={v}" for k, v in res["by_complexity"].items()))
    for r in res["rows"]:
        flag = "OK " if r["hit"] else "MISS"
        ans = (r["answer"] or r["error"] or "")[:60]
        print(f"    [{flag}] {r['case_id']}({r['expected_color']}) -> {ans}")


def main():
    ap = argparse.ArgumentParser(description="多模态 VLM 真实实测")
    ap.add_argument("--models", default=None,
                    help=f"VLM 模型逗号分隔；缺省 {','.join(DEFAULT_VLM_MODELS)}")
    ap.add_argument("--cases", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tests", "fixtures", "golden_cases.yaml"))
    ap.add_argument("--output", default=None, help="JSON 报告落盘路径（缺省不落盘）")
    ap.add_argument("--dry-run", action="store_true", help="只校验 key 状态与题目加载，不调用 LLM")
    args = ap.parse_args()

    cfg = load_config()
    images = ensure_images()
    cases = load_mm_cases(args.cases)
    scenarios = build_scenarios(cases, images)

    models = args.models.split(",") if args.models else DEFAULT_VLM_MODELS
    ready = [m for m in models if m in cfg.providers and key_available(cfg.providers[m])]
    skipped = [m for m in models if m not in ready]

    print(f"多模态题目: {len(cases)} 道（{len(scenarios)} 个测试场景）")
    print(f"测试图: {', '.join(os.path.basename(p) for p in images.values())} -> {IMG_DIR}")
    if skipped:
        print(f"未配 key 跳过: {skipped}")
    if not ready:
        print("无可用 VLM key（需配置 ZHIPU_API_KEY / DASHSCOPE_API_KEY / VOLCENGINE_API_KEY 等之一）。")
        print("--dry-run 已退出（未调用任何 LLM）。" if args.dry_run else "无可用模型，退出。")
        sys.exit(0 if args.dry_run else 2)
    print(f"可用 VLM 模型: {ready}")

    if args.dry_run:
        print("dry-run：题目/图片/key 检查通过，未调用 LLM。")
        for sc in scenarios:
            print(f"  - {sc['case_id']}: {os.path.basename(sc['image'])} 期望={sc['expected_keywords']}")
        return

    results = [run_model(cfg, m, scenarios) for m in ready]
    results.sort(key=lambda r: (r["accuracy"] or 0), reverse=True)
    for r in results:
        print_report(r)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"scenario_count": len(scenarios), "results": results}, f,
                      ensure_ascii=False, indent=2)
        print(f"\nJSON 报告 -> {args.output}")


if __name__ == "__main__":
    main()
