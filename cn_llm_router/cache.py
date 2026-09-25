"""分类结果内存缓存（ADR-0010）。

内存为主、TTL 失效、LRU 容量上限；cache_path 非 None 时提供 JSON 持久化接口
（v1 仅留接口，落盘不写过期时间戳，load 后按当前时刻重算 TTL）。
单进程串行使用，不加锁。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional


def _normalize(prompt: str) -> str:
    """规范化 prompt：strip + 折叠连续空白为单空格 + 转小写。"""
    return re.sub(r"\s+", " ", str(prompt)).strip().lower()


def make_cache_key(prompt: str, classifier_models, data_version: str) -> str:
    """生成缓存键：规范化 prompt + 分类模型链 + 数据版本，SHA-256 摘要。

    三者任意其一不同即生成不同键，避免模型链更换或数据升级后串用旧结果。
    """
    h = hashlib.sha256()
    h.update(_normalize(prompt).encode("utf-8"))
    h.update(b"\x1f")
    h.update("|".join(str(m) for m in classifier_models).encode("utf-8"))
    h.update(b"\x1f")
    h.update(str(data_version).encode("utf-8"))
    return h.hexdigest()


class ClassifyCache:
    """classify 结果的内存缓存。

    条目 = (过期单调时刻, Classification 的 dict 形式)；get 命中时 move_to_end
    以实现 LRU，set 超容量时 popitem(last=False) 淘汰最久未访问条目。
    ttl=0 表示写入即过期（测试/强制刷新用）。
    """

    def __init__(self, ttl: int = 3600, max_size: int = 1000, path: Optional[str] = None):
        self.ttl = ttl
        self.max_size = max_size
        self.path = path
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        if path:
            self.load(path)

    def _expiry(self) -> float:
        return time.monotonic() + self.ttl

    def get(self, key: str) -> Optional[dict]:
        item = self._store.get(key)
        if item is None:
            self._misses += 1
            return None
        expires_at, value = item
        # ttl<=0：写入即过期（强制刷新/测试用）；否则按单调时刻判定
        if self.ttl <= 0 or time.monotonic() > expires_at:
            del self._store[key]
            self._misses += 1
            return None
        self._store.move_to_end(key)  # LRU：命中即刷新为最近使用
        self._hits += 1
        return value

    def set(self, key: str, value: dict) -> None:
        self._store[key] = (self._expiry(), value)
        self._store.move_to_end(key)
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        # 命中统计保留（clear 只清数据，不清计数）

    @property
    def stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._store)}

    def load(self, path: str) -> None:
        """从 JSON 加载条目；文件不存在则空操作。load 后按当前时刻重算 TTL。"""
        p = Path(path)
        if not p.exists():
            return
        doc = json.loads(p.read_text(encoding="utf-8"))
        entries = doc.get("entries", {}) if isinstance(doc, dict) else {}
        base = time.monotonic()
        for k, v in entries.items():
            self._store[str(k)] = (base + self.ttl if self.ttl > 0 else 0.0, v)

    def save(self, path: Optional[str] = None) -> None:
        """保存到 JSON（不落盘过期时间戳）。"""
        p = Path(path or self.path)
        if p is None:
            raise ValueError("save() 需要 path（构造时未传 cache_path）")
        if p.parent and str(p.parent):
            p.parent.mkdir(parents=True, exist_ok=True)
        entries = {k: v[1] for k, v in self._store.items()}
        p.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
