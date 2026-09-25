"""使用统计与成本记录（ADR-0011）。

旁路记录：classify / route / completion 事件追加写 JSONL；
默认关闭（RouterConfig.stats_enabled=False），开启后由 scripts/cost_report.py 按月汇总。
不记录完整 prompt，只存 sha256 前 8 位 + 字符数。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger("cn_llm_router.stats")


def prompt_hash(prompt: str) -> str:
    """SHA-256 前 8 位，用于粗略去重，不泄露原文（ADR-0011 隐私节）。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]


class UsageRecorder:
    """把用量事件追加写入 JSONL；enabled=False 时空操作。

    v1 写操作用 threading.Lock 串行化，避免多线程交错写坏行。
    """

    def __init__(self, enabled: bool = False, log_path: str = "reports/usage.jsonl"):
        self.enabled = enabled
        self.log_path = log_path
        self._lock = threading.Lock()

    def record(self, event: dict) -> None:
        """追加一行 JSONL；任何写失败仅记 debug 日志，不影响主流程。"""
        if not self.enabled:
            return
        try:
            row = dict(event)
            row.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
            line = json.dumps(row, ensure_ascii=False)
            with self._lock:
                directory = os.path.dirname(self.log_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 —— 统计是旁路，失败不影响路由
            logger.debug("usage record 写入失败（忽略）: %s", e)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """chars→tokens 近似：len/4（英文约 4 char/token，中文折中）。ADR-0011 token 估算节。"""
        if not text:
            return 0
        return max(1, len(text) // 4)

    @staticmethod
    def estimate_cost(
        input_tokens: int,
        output_tokens: int,
        price_in: Optional[float],
        price_out: Optional[float],
    ) -> Optional[float]:
        """成本（元）= (input×price_in + output×price_out) / 1e6；缺价返回 None。

        与 ADR-0001 口径一致：price_in/out 单位为 元/百万 tokens。
        """
        if price_in is None or price_out is None:
            return None
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
