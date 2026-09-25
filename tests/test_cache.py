"""分类缓存测试（ADR-0010）：读写 / TTL / LRU / 命中重建 / 开关 / 键空间 / 统计 / 持久化。"""
import dataclasses
import time

from cn_llm_router import classify
from cn_llm_router.cache import ClassifyCache, make_cache_key
from cn_llm_router.classifier import Classifier


# ---- 底层 ClassifyCache ----

def test_basic_get_set():
    c = ClassifyCache(ttl=3600, max_size=10)
    assert c.get("missing") is None          # 未命中
    c.set("k", {"category": "数学推理", "complexity": "高"})
    assert c.get("k") == {"category": "数学推理", "complexity": "高"}


def test_ttl_expires():
    # ttl=0：写入即过期
    c = ClassifyCache(ttl=0)
    c.set("k", {"a": 1})
    assert c.get("k") is None

    # 极短 TTL + sleep：过期后视为未命中
    c2 = ClassifyCache(ttl=0.05)
    c2.set("k2", {"a": 1})
    assert c2.get("k2") == {"a": 1}
    time.sleep(0.1)
    assert c2.get("k2") is None


def test_max_size_lru_eviction():
    c = ClassifyCache(ttl=3600, max_size=2)
    c.set("k1", {"v": 1})
    c.set("k2", {"v": 2})
    c.get("k1")          # 访问 k1 → k1 变为最近使用，k2 最久未用
    c.set("k3", {"v": 3})  # 超容量，淘汰 k2
    assert "k1" in c._store
    assert "k2" not in c._store
    assert "k3" in c._store
    assert c.stats["size"] == 2


# ---- 键空间 ----

def test_make_cache_key_differs_by_input():
    base = make_cache_key("写 代码", ["M1", "M2"], "v1")
    # prompt 规范化：首尾/连续空白与大小写折叠不影响键
    assert make_cache_key("  写  代码 ", ["M1", "M2"], "v1") == base
    # prompt 内容不同
    assert make_cache_key("写文档", ["M1", "M2"], "v1") != base
    # 模型链不同
    assert make_cache_key("写代码", ["M1"], "v1") != base
    # 数据版本不同
    assert make_cache_key("写代码", ["M1", "M2"], "v2") != base


# ---- Classifier 集成 ----

def _with_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")


def test_cache_hit_returns_same_classification(data, cfg, monkeypatch):
    calls = []

    def fake(prompt, model):
        calls.append(model)
        return {"category": "数学推理", "complexity": "高",
                "confidence": 0.9, "second_guess": ""}

    _with_key(monkeypatch)
    clf = Classifier(data=data, cfg=cfg, llm_json=fake)
    r1 = clf.classify("证明费马小定理")
    r2 = clf.classify("证明费马小定理")

    assert len(calls) == 1                  # 第二次命中缓存，未再调 LLM
    assert r2.cached is True
    assert r1.category == r2.category == "数学推理"
    assert r1.complexity == r2.complexity == "高"
    assert r1.confidence == r2.confidence == 0.9


def test_cache_disabled_bypasses_cache(data, cfg, monkeypatch):
    calls = []

    def fake(prompt, model):
        calls.append(model)
        return {"category": "数学推理", "complexity": "高",
                "confidence": 0.9, "second_guess": ""}

    _with_key(monkeypatch)
    cfg_off = dataclasses.replace(cfg, cache_enabled=False)
    clf = Classifier(data=data, cfg=cfg_off, llm_json=fake)
    assert clf.cache is None
    clf.classify("证明费马小定理")
    clf.classify("证明费马小定理")
    assert len(calls) == 2                  # 关闭后每次都走 classify 逻辑


def test_stats_hit_miss_size(data, cfg, monkeypatch):
    calls = []

    def fake(prompt, model):
        calls.append(model)
        return {"category": "数学推理", "complexity": "高",
                "confidence": 0.9, "second_guess": ""}

    _with_key(monkeypatch)
    clf = Classifier(data=data, cfg=cfg, llm_json=fake)
    clf.classify("证明费马小定理")          # miss + set
    clf.classify("证明费马小定理")          # hit
    st = clf.cache.stats
    assert st["hits"] == 1
    assert st["misses"] == 1
    assert st["size"] == 1


def test_rule_fallback_result_also_cached(data, cfg):
    """离线规则兜底结果也写入缓存；显式注入同一 cache 验证第二次命中。"""
    shared = ClassifyCache(ttl=3600, max_size=10)
    clf = Classifier(data=data, cfg=cfg, cache=shared)
    r1 = clf.classify("帮我写一个Python函数解析JSON")
    st1 = dict(shared.stats)
    r2 = clf.classify("帮我写一个Python函数解析JSON")
    assert r1.category == r2.category == "程序编码"
    assert r2.cached is True
    assert shared.stats["hits"] == st1["hits"] + 1


# ---- 持久化 ----

def test_save_load_roundtrip(tmp_path):
    c = ClassifyCache(ttl=3600, max_size=10)
    c.set("k1", {"category": "数学推理", "complexity": "高", "confidence": 0.9})
    c.set("k2", {"category": "翻译润色", "complexity": "低", "confidence": 0.7})
    p = tmp_path / "cache.json"
    c.save(str(p))
    assert p.exists()

    c2 = ClassifyCache(ttl=3600, max_size=10)
    c2.load(str(p))
    assert c2.get("k1") == {"category": "数学推理", "complexity": "高", "confidence": 0.9}
    assert c2.get("k2")["category"] == "翻译润色"


def test_empty_prompt_uncached_output_unchanged(data, cfg):
    """空请求走默认兜底；缓存开启不改变返回语义。"""
    r = classify("   ", config=cfg)
    assert r.category == "知识问答/检索"
    assert r.complexity == "中"
    assert "输入为空" in r.fallback_reason
