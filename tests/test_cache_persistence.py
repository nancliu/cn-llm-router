"""缓存持久化测试（ADR-0014）：往返命中 / 过期剔除 / 文件格式 / 无盘 / auto_save / 旧格式兼容。"""
import json
import time

from cn_llm_router.cache import ClassifyCache


def test_save_load_roundtrip_new_instance(tmp_path):
    """跨进程语义：c1 保存后，全新 c2(path=...) 构造即加载并命中。"""
    c1 = ClassifyCache(ttl=3600, max_size=10)
    c1.set("k1", {"category": "数学推理", "complexity": "高", "confidence": 0.9})
    c1.set("k2", {"category": "翻译润色", "complexity": "低", "confidence": 0.7})
    p = tmp_path / "cache.json"
    c1.save(str(p))

    c2 = ClassifyCache(ttl=3600, max_size=10, path=str(p))  # 构造自动 load
    assert c2.get("k1") == {"category": "数学推理", "complexity": "高", "confidence": 0.9}
    assert c2.get("k2")["category"] == "翻译润色"


def test_expired_entries_pruned_on_load(tmp_path):
    """created_at 已超过 TTL 的条目，加载后被剔除、不可命中。"""
    c1 = ClassifyCache(ttl=3600, max_size=10)
    c1.set("fresh", {"v": 1})
    c1.set("stale", {"v": 2})
    p = tmp_path / "cache.json"
    c1.save(str(p))

    # 把 stale 的 created_at 改成 2 小时前（ttl=3600 已过期）
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["entries"]["stale"]["created_at"] = time.time() - 7200
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    c2 = ClassifyCache(ttl=3600, max_size=10, path=str(p))
    assert c2.get("stale") is None      # 过期条目不复活
    assert c2.get("fresh") == {"v": 1}  # 新鲜条目正常加载
    assert c2.stats["size"] == 1


def test_file_format_is_versioned_json(tmp_path):
    """持久化文件可被 json 解析，含 version 与 {value, created_at} 结构。"""
    c = ClassifyCache(ttl=3600, max_size=10)
    c.set("k", {"category": "数据分析", "complexity": "中"})
    p = tmp_path / "cache.json"
    c.save(str(p))
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    entry = doc["entries"]["k"]
    assert entry["value"] == {"category": "数据分析", "complexity": "中"}
    assert isinstance(entry["created_at"], (int, float))
    assert time.time() - entry["created_at"] < 60  # 刚刚写入


def test_no_disk_when_path_none(tmp_path):
    """cache_path=None 时纯内存：save() 报错、load() 不产生文件、构造不碰盘。"""
    c = ClassifyCache(ttl=3600, max_size=10)
    c.set("k", {"v": 1})
    import pytest
    with pytest.raises(ValueError):
        c.save()  # 无 path 且未显式传路径
    ghost = tmp_path / "nope.json"
    c.load(str(ghost))  # 文件不存在，空操作
    assert not ghost.exists()
    assert c.get("k") == {"v": 1}  # 内存行为不受影响


def test_auto_save_writes_on_set(tmp_path):
    """auto_save=True 时 set 后文件立即更新。"""
    p = tmp_path / "cache.json"
    c = ClassifyCache(ttl=3600, max_size=10, path=str(p), auto_save=True)
    assert not p.exists() or p.stat().st_size == 0  # 空文件或尚未创建
    c.set("k1", {"v": 1})
    assert p.exists()
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["entries"]["k1"]["value"] == {"v": 1}
    c.set("k2", {"v": 2})
    doc2 = json.loads(p.read_text(encoding="utf-8"))
    assert set(doc2["entries"]) == {"k1", "k2"}


def test_legacy_flat_format_compat(tmp_path):
    """旧格式（entry 直接是 value dict）可加载，按刚创建授予完整 TTL。"""
    p = tmp_path / "cache.json"
    p.write_text(
        json.dumps({"version": 1, "entries": {"old": {"v": "legacy"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    c = ClassifyCache(ttl=3600, max_size=10, path=str(p))
    assert c.get("old") == {"v": "legacy"}
