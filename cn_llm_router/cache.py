"""分类结果内存缓存（ADR-0010）+ 持久化（ADR-0014）。

内存为主、TTL 失效、LRU 容量上限；cache_path 非 None 时 JSON 持久化：
盘上条目记录 created_at（wall clock），加载时按剩余 TTL 复活，已过期条目不加载。
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

    内部条目 = (过期单调时刻, 创建 wall 时刻, Classification 的 dict 形式)；
    get 命中时 move_to_end 以实现 LRU，set 超容量时 popitem(last=False) 淘汰最久未访问条目。
    ttl=0 表示写入即过期（测试/强制刷新用）。

    持久化（ADR-0014）：path 非 None 时构造即 load()；save()/flush() 写 JSON；
    auto_save=True 时每次 set() 后同步写盘。
    """

    def __init__(
        self,
        ttl: int = 3600,
        max_size: int = 1000,
        path: Optional[str] = None,
        auto_save: bool = False,
    ):
        self.ttl = ttl
        self.max_size = max_size
        self.path = path
        self.auto_save = auto_save
        self._store: OrderedDict[str, tuple[float, float, dict]] = OrderedDict()
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
        expires_at, _created, value = item
        # ttl<=0：写入即过期（强制刷新/测试用）；否则按单调时刻判定
        if self.ttl <= 0 or time.monotonic() > expires_at:
            del self._store[key]
            self._misses += 1
            return None
        self._store.move_to_end(key)  # LRU：命中即刷新为最近使用
        self._hits += 1
        return value

    def set(self, key: str, value: dict) -> None:
        self._store[key] = (self._expiry(), time.time(), value)
        self._store.move_to_end(key)
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)
        if self.auto_save and self.path:
            self.save()

    def flush(self) -> None:
        """主动把内存缓存写盘（auto_save=False 时的落盘入口）。"""
        self.save()

    def clear(self) -> None:
        self._store.clear()
        # 命中统计保留（clear 只清数据，不清计数）

    @property
    def stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._store)}

    def load(self, path: str) -> None:
        """从 JSON 加载条目；文件不存在则空操作。

        新格式条目：{"value": {...}, "created_at": <unix秒>}；按剩余 TTL 复活，
        已过期条目不加载。旧格式（entry 直接为 value dict）按刚创建处理。
        """
        p = Path(path)
        if not p.exists():
            return
        doc = json.loads(p.read_text(encoding="utf-8"))
        entries = doc.get("entries", {}) if isinstance(doc, dict) else {}
        now_wall = time.time()
        now_mono = time.monotonic()
        for k, v in entries.items():
            if isinstance(v, dict) and "value" in v and "created_at" in v:
                value = v["value"]
                created = float(v["created_at"])
            else:  # 旧格式：无时间戳，按刚创建授予完整 TTL
                value = v
                created = now_wall
            if self.ttl <= 0:
                continue  # 写入即过期策略：不复活任何条目
            age = now_wall - created
            if age >= self.ttl:
                continue  # 已过期，不加载
            self._store[str(k)] = (now_mono + (self.ttl - age), created, value)

    def save(self, path: Optional[str] = None) -> None:
        """保存到 JSON：每条目附带 created_at（wall clock），供跨进程 TTL 计算。"""
        p = path or self.path
        if not p:
            raise ValueError("save() 需要 path（构造时未传 cache_path）")
        p = Path(p)
        if p.parent and str(p.parent):
            p.parent.mkdir(parents=True, exist_ok=True)
        entries = {k: {"value": v[2], "created_at": v[1]} for k, v in self._store.items()}
        p.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
