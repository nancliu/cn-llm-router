#!/usr/bin/env python3
"""多模态（VLM）真实实测脚本（P1-4，ADR-0015）。

用标准库（struct/zlib）生成测试图，调用 OpenAI 兼容多模态 messages 接口，
按关键词族命中判定答案正确性，输出每模型准确率与入库格式 CSV。

v2 场景集（20 题，ADR-0015）：
- 颜色识别 4 题（纯色块，低）
- OCR 3 题（5x7 点阵字渲染 "7"/"B"/"A3"，低/中）
- 形状识别 3 题（白底实心圆/三角/矩形，中）
- 计数 3 题（2/3/5 个色块，中）
- 颜色+形状组合 4 题（两族关键词须同时命中，高）
- 按颜色计数 3 题（颜色族+数字族 AND，高）

评分：expected_groups（关键词族）全部命中即正确；VLM 实测分 = 准确率×100。

用法:
    python scripts/eval_multimodal.py [--models Qwen3.8-Max-0902,DeepSeek-V4.1-Flash-CED] \
        [--output reports/mm-eval-<ts>.json] [--output-csv data/vlm_scores.csv] [--dry-run]
"""
import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cn_llm_router.config import load_config, key_available  # noqa: E402

IMG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tests", "fixtures", "mm_images")

CATEGORY = "多模态理解"
DEFAULT_VLM_MODELS = ["GLM-5.3-Flash", "Qwen3.8-Max-0902", "DeepSeek-V4.1-Flash-CED"]

# ---------------------------------------------------------------------------
# 颜色 / 形状 关键词族
# ---------------------------------------------------------------------------
RED = ["红色", "红", "red"]
GREEN = ["绿色", "绿", "green"]
BLUE = ["蓝色", "蓝", "blue"]
YELLOW = ["黄色", "黄", "yellow"]
CIRCLE = ["圆形", "圆圈", "圆", "circle"]
TRIANGLE = ["三角形", "三角", "triangle"]
RECT = ["矩形", "长方形", "正方形", "方形", "rectangle", "rect"]
_NUM_KW = {
    "1": ["1", "一"],
    "2": ["2", "二", "两"],
    "3": ["3", "三"],
    "4": ["4", "四"],
    "5": ["5", "五"],
}

# ---------------------------------------------------------------------------
# 标准库 PNG 写入（无 PIL 依赖）
# ---------------------------------------------------------------------------

def _chunk(typ: bytes, data: bytes) -> bytes:
    c = typ + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)


def write_png(path: str, width: int, height: int, pixels: bytes) -> None:
    """pixels: row-major RGB bytes，长度 width*height*3。"""
    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3]
                   for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr))
        f.write(_chunk(b"IDAT", zlib.compress(raw)))
        f.write(_chunk(b"IEND", b""))


def _blank(width: int, height: int, bg: tuple[int, int, int]) -> bytearray:
    row = bytes(bg) * width
    return bytearray(row * height)


def _put(px: bytearray, width: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < width:
        off = (y * width + x) * 3
        px[off:off + 3] = bytes(color)


# ---------------------------------------------------------------------------
# 测试图生成
# ---------------------------------------------------------------------------

def make_color_png(path: str, rgb: tuple[int, int, int], size: int = 128) -> None:
    write_png(path, size, size, bytes(rgb) * (size * size))


def make_shape_png(path: str, shape: str, fg: tuple[int, int, int],
                   bg: tuple[int, int, int] = (255, 255, 255), size: int = 160) -> None:
    px = _blank(size, size, bg)
    cx = cy = size // 2
    for y in range(size):
        for x in range(size):
            hit = False
            if shape == "circle":
                hit = (x - cx) ** 2 + (y - cy) ** 2 <= 55 ** 2
            elif shape == "triangle":
                # 顶点 (cx,35) 底边 (45,125)-(115,125)
                hit = (y >= 35 and y <= 125 and
                       abs(x - cx) <= (y - 35) * 35 / 90)
            elif shape == "rect":
                hit = 45 <= x <= 115 and 45 <= y <= 115
            if hit:
                _put(px, size, x, y, fg)
    write_png(path, size, size, bytes(px))


def make_count_png(path: str, squares: list[tuple[int, int, int]],
                   bg: tuple[int, int, int] = (255, 255, 255),
                   size: int = 180) -> None:
    """squares: 每个色块的颜色（从左到右排列）。"""
    px = _blank(size, size, bg)
    sq = 40
    n = len(squares)
    gap = 12
    total = n * sq + (n - 1) * gap
    x0 = (size - total) // 2
    y0 = (size - sq) // 2
    for i, color in enumerate(squares):
        for y in range(y0, y0 + sq):
            for x in range(x0 + i * (sq + gap), x0 + i * (sq + gap) + sq):
                _put(px, size, x, y, color)
    write_png(path, size, size, bytes(px))


# 5x7 点阵字（v1：0-9 A B C）
FONT_5X7 = {
    "0": ["01111", "10001", "10001", "10001", "10001", "10001", "01111"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["01110", "10001", "00001", "00110", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
}


def make_text_png(path: str, text: str, fg: tuple[int, int, int],
                  bg: tuple[int, int, int] = (255, 255, 255), scale: int = 6) -> None:
    glyph_w, glyph_h = 5, 7
    gap = 1
    w = (glyph_w * scale) * len(text) + gap * scale * (len(text) - 1) + 2 * scale
    h = glyph_h * scale + 2 * scale
    px = _blank(w, h, bg)
    for ci, ch in enumerate(text.upper()):
        glyph = FONT_5X7.get(ch)
        if not glyph:
            continue
        gx0 = scale + ci * (glyph_w * scale + gap * scale)
        gy0 = scale
        for ry, rowbits in enumerate(glyph):
            for rx, bit in enumerate(rowbits):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            _put(px, w, gx0 + rx * scale + dx, gy0 + ry * scale + dy, fg)
    write_png(path, w, h, bytes(px))


# ---------------------------------------------------------------------------
# 场景集（20 题）
# ---------------------------------------------------------------------------

def build_scenarios() -> list[dict]:
    """生成 20 个 VLM 测试场景；图片不存在则现场生成。"""
    os.makedirs(IMG_DIR, exist_ok=True)
    sc: list[dict] = []

    def add(cid: str, cx: str, kind: str, prompt: str, img: str,
            groups: list[list[str]], hint: str) -> None:
        sc.append({
            "id": cid, "complexity": cx, "kind": kind,
            "prompt": prompt, "image": img,
            "expected_groups": groups, "hint": hint,
        })

    # --- 低：纯色块颜色识别（4） ---
    colors = [("red", RED, (220, 40, 40)), ("green", GREEN, (40, 160, 80)),
              ("blue", BLUE, (40, 80, 220)), ("yellow", YELLOW, (230, 200, 50))]
    for name, kws, rgb in colors:
        p = os.path.join(IMG_DIR, f"color_{name}.png")
        if not os.path.exists(p):
            make_color_png(p, rgb)
        add(f"mm-color-{name}", "低", "color",
            "这张图片的主色调是什么颜色？只需回答颜色名称。",
            p, [kws], name)

    # --- 低：OCR 单字符（2） ---
    for ch in ("7", "B"):
        p = os.path.join(IMG_DIR, f"ocr_{ch}.png")
        if not os.path.exists(p):
            make_text_png(p, ch, (20, 20, 20))
        add(f"mm-ocr-{ch}", "低", "ocr",
            "请识别图片中的字符，只输出字符本身，不要解释。",
            p, [[ch.lower()]], ch)

    # --- 中：形状识别（3） ---
    shape_defs = [("circle", CIRCLE, (220, 40, 40)), ("triangle", TRIANGLE, (40, 80, 220)),
                  ("rect", RECT, (40, 160, 80))]
    for name, kws, rgb in shape_defs:
        p = os.path.join(IMG_DIR, f"shape_{name}.png")
        if not os.path.exists(p):
            make_shape_png(p, name, rgb)
        add(f"mm-shape-{name}", "中", "shape",
            "这张图片中央是什么形状？回答形状名称（圆形/三角形/矩形）。",
            p, [kws], name)

    # --- 中：计数（3） ---
    count_defs = [(2, (220, 40, 40)), (3, (40, 80, 220)), (5, (40, 160, 80))]
    for n, rgb in count_defs:
        p = os.path.join(IMG_DIR, f"count_{n}.png")
        if not os.path.exists(p):
            make_count_png(p, [rgb] * n)
        add(f"mm-count-{n}", "中", "count",
            "这张图片中有几个同色色块？只回答数字。",
            p, [_NUM_KW[str(n)]], str(n))

    # --- 中：OCR 双字符（1） ---
    p = os.path.join(IMG_DIR, "ocr_a3.png")
    if not os.path.exists(p):
        make_text_png(p, "A3", (20, 20, 20))
    add("mm-ocr-a3", "中", "ocr",
        "请识别图片中的字符并原样输出，不要解释。",
        p, [["a3"]], "A3")

    # --- 高：颜色+形状组合（4，两族须同时命中） ---
    combo_defs = [("circle", RED, (220, 40, 40), "红色圆形"), ("triangle", BLUE, (40, 80, 220), "蓝色三角形"),
                  ("rect", GREEN, (40, 160, 80), "绿色矩形"), ("circle", YELLOW, (230, 200, 50), "黄色圆形")]
    for i, (shape, kws, rgb, hint) in enumerate(combo_defs):
        p = os.path.join(IMG_DIR, f"combo_{i}_{shape}.png")
        if not os.path.exists(p):
            make_shape_png(p, shape, rgb)
        add(f"mm-combo-{i}", "高", "combo",
            "这张图片中央是什么形状、什么颜色？请同时回答形状和颜色。",
            p, [kws, {"circle": CIRCLE, "triangle": TRIANGLE, "rect": RECT}[shape]], hint)

    # --- 高：按颜色计数（3；题目要求只回答数字，评分只判数字族） ---
    RGB_RED, RGB_BLUE, RGB_GREEN = (220, 40, 40), (40, 80, 220), (40, 160, 80)
    cc_defs = [
        ("red3", RED, 3, [RGB_RED] * 3 + [RGB_BLUE] * 2, "3红2蓝"),
        ("blue2", BLUE, 2, [RGB_BLUE] * 2 + [RGB_RED] * 3, "2蓝3红"),
        ("green4", GREEN, 4, [RGB_GREEN] * 4 + [RGB_RED] * 1, "4绿1红"),
    ]
    for cid, color_kw, n, squares, hint in cc_defs:
        p = os.path.join(IMG_DIR, f"countcolor_{cid}.png")
        if not os.path.exists(p):
            make_count_png(p, squares, size=220)
        add(f"mm-countcolor-{cid}", "高", "countcolor",
            f"这张图片中有几个{color_kw[0]}色块？只回答数字。",
            p, [_NUM_KW[str(n)]], hint)

    return sc


# ---------------------------------------------------------------------------
# VLM 调用与评分
# ---------------------------------------------------------------------------

def img_to_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def ask_vlm(cfg, model: str, scenario: dict) -> str:
    from cn_llm_router.gateway import build_client

    prov = cfg.providers[model]
    client = build_client(prov)
    resp = client.chat.completions.create(
        model=prov.api_model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": scenario["prompt"]},
                {"type": "image_url", "image_url": {"url": img_to_data_uri(scenario["image"])}},
            ],
        }],
        temperature=0,
        max_tokens=128,
        timeout=prov.timeout,
    )
    return (resp.choices[0].message.content or "").strip()


def score(answer: str, groups: list[list[str]]) -> bool:
    """全部关键词族至少命中一个关键词（去空格、小写）。"""
    low = re.sub(r"\s+", "", answer.lower())
    return all(any(kw.lower() in low for kw in g) for g in groups)


def run_model(cfg, model: str, scenarios: list[dict]) -> dict:
    rows, t0 = [], time.time()
    for sc in scenarios:
        try:
            ans = ask_vlm(cfg, model, sc)
            hit = score(ans, sc["expected_groups"])
            err = None
        except Exception as e:  # noqa: BLE001 —— 单题失败不中断整模型
            ans, hit, err = "", False, f"{type(e).__name__}: {e}"
        rows.append({
            "id": sc["id"], "complexity": sc["complexity"], "kind": sc["kind"],
            "image": os.path.basename(sc["image"]), "hint": sc["hint"],
            "answer": ans, "hit": hit, "error": err,
        })
    elapsed = time.time() - t0
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    by_cx: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        by_cx[r["complexity"]][1] += 1
        by_cx[r["complexity"]][0] += 1 if r["hit"] else 0
    return {
        "model": model, "n": n, "elapsed_s": round(elapsed, 1),
        "accuracy": round(hits / n, 4) if n else None,
        "by_complexity": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cx.items())},
        "by_complexity_hits": {k: v for k, v in sorted(by_cx.items())},
        "rows": rows,
    }


def print_report(res: dict) -> None:
    print(f"\n== {res['model']} ==")
    print(f"  准确率: {res['accuracy']:.1%}  ({res['n']} 题, {res['elapsed_s']}s)")
    print("  按复杂度:", ", ".join(f"{k}={v}" for k, v in res["by_complexity"].items()))
    for r in res["rows"]:
        flag = "OK " if r["hit"] else "MISS"
        ans = (r["answer"] or r["error"] or "")[:60]
        print(f"    [{flag}] {r['id']}({r['hint']}) -> {ans}")


# ---------------------------------------------------------------------------
# 入库 CSV（ADR-0015）
# ---------------------------------------------------------------------------

def write_vlm_csv(results: list[dict], path: str, eval_date: str, json_report: str | None) -> None:
    import csv
    source = f"scripts/eval_multimodal.py (ADR-0015 实测; {os.path.basename(json_report) if json_report else ''}; {eval_date})"
    fieldnames = ["model", "category", "complexity", "score", "basis",
                  "num_questions", "eval_date", "source_url", "as_of"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for res in results:
            n_total = res["n"]
            for cx, (h, t) in sorted(res["by_complexity_hits"].items()):
                if t == 0:
                    continue
                w.writerow({
                    "model": res["model"],
                    "category": CATEGORY,
                    "complexity": cx,
                    "score": round(h / t * 100, 1),
                    "basis": f"VLM真实实测; n={n_total}; accuracy×100",
                    "num_questions": n_total,
                    "eval_date": eval_date,
                    "source_url": source,
                    "as_of": eval_date,
                })


def main():
    from datetime import datetime

    ap = argparse.ArgumentParser(description="多模态 VLM 真实实测（ADR-0015）")
    ap.add_argument("--models", default=None,
                    help=f"VLM 模型逗号分隔；缺省 {','.join(DEFAULT_VLM_MODELS)}")
    ap.add_argument("--output", default=None, help="JSON 报告落盘路径（缺省不落盘）")
    ap.add_argument("--output-csv", default=None,
                    help="入库格式 CSV 路径（data/vlm_scores.csv）")
    ap.add_argument("--dry-run", action="store_true", help="只校验 key 状态与题目加载，不调用 LLM")
    args = ap.parse_args()

    cfg = load_config()
    scenarios = build_scenarios()

    models = args.models.split(",") if args.models else DEFAULT_VLM_MODELS
    ready = [m for m in models if m in cfg.providers and key_available(cfg.providers[m])]
    skipped = [m for m in models if m not in ready]

    print(f"VLM 实测题集: {len(scenarios)} 题（低/中/高 = "
          f"{sum(1 for s in scenarios if s['complexity']=='低')}/"
          f"{sum(1 for s in scenarios if s['complexity']=='中')}/"
          f"{sum(1 for s in scenarios if s['complexity']=='高')}）")
    print(f"测试图目录: {IMG_DIR}")
    if skipped:
        print(f"未配 key 跳过: {skipped}")
    if not ready:
        print("无可用 VLM key。")
        print("--dry-run 已退出（未调用任何 LLM）。" if args.dry_run else "无可用模型，退出。")
        sys.exit(0 if args.dry_run else 2)
    print(f"可用 VLM 模型: {ready}")

    if args.dry_run:
        print("dry-run：题目/图片/key 检查通过，未调用 LLM。")
        for sc in scenarios:
            print(f"  - [{sc['complexity']}] {sc['id']}: {os.path.basename(sc['image'])} 期望={sc['hint']}")
        return

    results = [run_model(cfg, m, scenarios) for m in ready]
    results.sort(key=lambda r: (r["accuracy"] or 0), reverse=True)
    for r in results:
        print_report(r)

    eval_date = datetime.now().strftime("%Y-%m-%d")
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"scenario_count": len(scenarios), "eval_date": eval_date,
                       "results": results}, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告 -> {args.output}")
    if args.output_csv:
        write_vlm_csv(results, args.output_csv, eval_date, args.output)
        print(f"入库 CSV -> {args.output_csv}")


if __name__ == "__main__":
    main()
