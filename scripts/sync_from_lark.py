#!/usr/bin/env python3
"""飞书打分表 → data/*.csv 同步脚本（README backlog 第 1 项）。

链路: lark-cli sheets +table-get --output-path <快照.json> → export_data.parse_sheets → 差异检测 → 落盘。
幂等: 表格未变化时重跑不改写任何文件（内容一致即跳过，保留 mtime 与 VERSION）。

用法:
    python3 scripts/sync_from_lark.py --url <打分表URL> [--data-dir data] [--force]
    - --url 缺省时读环境变量 CN_LLM_ROUTER_SHEET_URL
    - --force 强制重写（即使内容一致也落盘并更新时间戳）
依赖: lark-cli（sheet skill 提供的飞书表格 CLI）、export_data.py（同目录）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_data  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_DATA_DIR = os.path.join(REPO_ROOT, "data")
DEFAULT_URL_ENV = "CN_LLM_ROUTER_SHEET_URL"


def fetch_snapshot(url: str, out_json: str) -> None:
    """调用 lark-cli 拉取全量快照到 out_json。"""
    cmd = ["lark-cli", "sheets", "+table-get", "--url", url, "--output-path", out_json]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        sys.exit(f"lark-cli +table-get 失败（{proc.returncode}）: {proc.stderr[:500]}")
    with open(out_json, encoding="utf-8") as f:
        doc = json.load(f)
    if not doc.get("complete"):
        sys.exit("快照不完整（complete=false）：表数据被截断，请检查 lark-cli 输出。")
    required = {"模型注册表", "能力评分矩阵", "综合与性价比"}
    names = {s["name"] for s in doc["sheets"]}
    missing = required - names
    if missing:
        sys.exit(f"打分表缺少子表: {sorted(missing)}（实际: {sorted(names)}）")


def file_content(path: str) -> str:
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def sync(data_dir: str, doc: dict, force: bool) -> dict:
    """解析并落盘；返回变更报告。幂等：内容一致时跳过（force 时仅重写数据文件、不动 VERSION）。"""
    parsed = export_data.parse_sheets(doc)

    # 用临时目录生成新文件，逐文件对比现有内容
    with tempfile.TemporaryDirectory() as tmp:
        export_data.write_data(tmp, parsed)
        changed, unchanged = [], []
        for fname in export_data.OUTPUT_FILES:
            new_path = os.path.join(tmp, fname)
            cur_path = os.path.join(data_dir, fname)
            new_content = file_content(new_path)
            same = os.path.exists(cur_path) and file_content(cur_path) == new_content
            (unchanged if same else changed).append(fname)
        if changed:
            for fname in changed:
                shutil.copyfile(os.path.join(tmp, fname), os.path.join(data_dir, fname))
            # 数据有更新：在 VERSION 追加同步时间戳（版本号保持数据版本）
            sync_stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            vpath = os.path.join(data_dir, "VERSION")
            with open(vpath, "a", encoding="utf-8") as f:
                f.write(f"synced_at: {sync_stamp}\n")
        elif force:
            # 内容一致但用户强制重写：只重写数据文件，不伪造 VERSION 同步时间戳
            for fname in export_data.OUTPUT_FILES:
                if fname != "VERSION":
                    shutil.copyfile(os.path.join(tmp, fname), os.path.join(data_dir, fname))
    n_score = sum(1 for s in parsed["scores"] if s["score"] != "")
    return {
        "models": len(parsed["models"]),
        "scores_total": len(parsed["scores"]),
        "scores_valid": n_score,
        "weights": len(parsed["weights"]),
        "categories": len(parsed["categories"]),
        "changed": changed,
        "unchanged": unchanged,
    }


def main():
    ap = argparse.ArgumentParser(description="飞书打分表 → data/*.csv 同步")
    ap.add_argument("--url", default=os.environ.get(DEFAULT_URL_ENV, ""), help=f"打分表链接（缺省读 {DEFAULT_URL_ENV}）")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--force", action="store_true", help="内容一致也强制重写数据文件（VERSION 不变）")
    ap.add_argument("--keep-snapshot", default=None, help="调试：保留快照 JSON 到该路径")
    args = ap.parse_args()
    if not args.url:
        sys.exit(f"缺少打分表 URL：传 --url 或设置环境变量 {DEFAULT_URL_ENV}")

    with tempfile.TemporaryDirectory() as tmpd:
        snap = args.keep_snapshot or os.path.join(tmpd, "sheet_snapshot.json")
        print(f"[1/3] 拉取打分表快照 ...")
        fetch_snapshot(args.url, snap)
        with open(snap, encoding="utf-8") as f:
            doc = json.load(f)
        print(f"[2/3] 校验并解析（子表 {len(doc['sheets'])} 张）")
        report = sync(args.data_dir, doc, args.force)
        print(f"[3/3] 同步完成")
        print(f"  models={report['models']}  scores={report['scores_total']}（有效 {report['scores_valid']}）"
              f"  weights={report['weights']}  categories={report['categories']}")
        if report["changed"]:
            print(f"  更新文件: {', '.join(report['changed'])}（VERSION 已记录 synced_at）")
        elif args.force:
            print("  内容一致，按 --force 重写数据文件（VERSION 未变）")
        else:
            print("  内容一致，无变化（幂等，未改写任何文件）")
        if args.keep_snapshot:
            print(f"  快照已保留: {snap}")


if __name__ == "__main__":
    main()
