#!/usr/bin/env python3
"""启动 cn-llm-router 本地面板（ADR-0017）。

用法:
    python scripts/start_web.py [--host 127.0.0.1] [--port 8765] [--config-dir ...]

仅监听 127.0.0.1，不暴露公网。浏览器打开打印的访问地址即可。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cn_llm_router.config import load_config  # noqa: E402
from cn_llm_router.web import run_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="cn-llm-router 本地面板（ADR-0017）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1，不建议改绑公网）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    ap.add_argument("--config-dir", default=None,
                    help="配置目录（缺省 config/ 或 CN_LLM_ROUTER_CONFIG）")
    args = ap.parse_args()

    cfg = load_config(args.config_dir)
    print(f"访问地址：http://{args.host}:{args.port}")
    run_server(host=args.host, port=args.port, config=cfg)


if __name__ == "__main__":
    main()
