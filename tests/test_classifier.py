"""分类器测试：LLM 判类（mock）、规则兜底、默认兜底（ADR-0002）。"""
import pytest

from cn_llm_router import classify
from cn_llm_router.classifier import Classifier
from cn_llm_router.data_loader import RouterData


def _clf(data, cfg, llm_json, debug=False):
    return Classifier(data=data, cfg=cfg, debug=debug, llm_json=llm_json)


# ---- LLM 判类（mock；需注入 key 才会走到 LLM 候选链） ----

def _with_key(monkeypatch):
    # 默认分类候选链第一候选 Qwen3.8-Max-0902（key_env=DASHSCOPE_API_KEY）
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")


def test_llm_valid_json(data, cfg, monkeypatch):
    def fake(prompt, model):
        return {"category": "数学推理", "complexity": "高", "confidence": 0.9, "second_guess": ""}

    _with_key(monkeypatch)
    res = _clf(data, cfg, fake).classify("证明费马小定理")
    assert res.category == "数学推理"
    assert res.complexity == "高"
    assert res.confidence == 0.9
    assert not res.low_confidence
    assert res.fallback_reason is None


def test_llm_debug_raw(data, cfg, monkeypatch):
    def fake(prompt, model):
        return {"category": "翻译润色", "complexity": "中", "confidence": 0.8, "raw_reason": "含翻译"}

    _with_key(monkeypatch)
    res = _clf(data, cfg, fake, debug=True).classify("翻译这句话")
    assert res.raw == {"category": "翻译润色", "complexity": "中", "confidence": 0.8, "raw_reason": "含翻译"}
    assert res.category == "翻译润色"


def test_llm_invalid_category_falls_back(data, cfg, monkeypatch):
    def fake(prompt, model):
        return {"category": "不存在的类别", "complexity": "高", "confidence": 0.9}

    _with_key(monkeypatch)
    res = _clf(data, cfg, fake).classify("随便")
    assert res.low_confidence is True
    assert res.fallback_reason is not None


def test_llm_raises_uses_rule_fallback(data, cfg, monkeypatch):
    def fake(prompt, model):
        raise RuntimeError("上游不可用")

    _with_key(monkeypatch)
    res = _clf(data, cfg, fake).classify("帮我写一个Python函数解析JSON")
    assert res.category == "程序编码"
    assert res.fallback_reason == "规则关键词兜底"


# ---- 规则兜底 ----

@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("帮我写一个Python函数解析JSON", "程序编码"),
        ("做一个登录页的HTML和CSS", "前端UX设计"),
        ("帮我写一份本周工作周报", "文书撰写"),
        ("用SQL统计每天订单数量", "数据分析"),
        ("证明费马小定理", "数学推理"),
        ("把这个任务拆成三个子agent执行", "Agent编排"),
        ("给这篇50页论文写个摘要", "长文档处理"),
        ("把这段话翻译成英文", "翻译润色"),
        ("识别这张图片里的文字", "多模态理解"),
        ("什么是贝叶斯定理", "知识问答/检索"),
        ("给新奶茶店起5个名字", "创意生成"),
        ("这段代码为什么报错", "代码审查与调试"),
    ],
)
def test_rule_fallback_hits_all_categories(data, cfg, prompt, expected):
    """12 类各一条关键词明确的请求，规则兜底应命中（无 LLM key 时的离线路径）。"""
    res = classify(prompt, config=cfg)
    assert res.category == expected, f"{prompt!r} -> {res.category}（期望 {expected}）"
    assert res.low_confidence is True


def test_rule_fallback_longest_keyword_wins(data, cfg):
    # "表格组件 hover" 命中 前端UX（组件）且无其它更长关键词 → 应为 前端UX设计
    res = classify("给这个表格组件加上hover高亮", config=cfg)
    assert res.category == "前端UX设计"


def test_rule_fallback_generic_word_does_not_misfire(data, cfg):
    # "计算" 是泛词：知识问答含更具体的"区别/是什么"时应判知识问答
    res = classify("量子计算和经典计算在原理上的区别是什么", config=cfg)
    assert res.category == "知识问答/检索"


# ---- 默认兜底 ----

def test_empty_prompt_default(data, cfg):
    res = classify("   ", config=cfg)
    assert res.category == "知识问答/检索"
    assert res.complexity == "中"
    assert res.low_confidence is True
    assert "输入为空" in res.fallback_reason


def test_no_llm_no_rule_default(data, cfg):
    """无 LLM key 且无关键词命中 → 默认兜底。"""
    res = classify("本周五下午三点", config=cfg)
    assert res.category == "知识问答/检索"
    assert res.fallback_reason is not None


# ---- golden cases 覆盖率 ----

def test_golden_cases_rule_testable_subset(data, cfg):
    """rule_testable=true 的 golden cases 离线断言命中期望类别（规则兜底）。"""
    from conftest import load_golden_cases

    for case in load_golden_cases():
        if not case.get("rule_testable"):
            continue
        res = classify(case["prompt"], config=cfg)
        assert res.category == case["category"], (
            f"{case['id']} {case['prompt']!r} -> {res.category}（期望 {case['category']}）"
        )
        # 规则兜底无法判断复杂度（恒为中）；仅当期望复杂度就是"中"时断言
        if case.get("complexity") == "中":
            assert res.complexity == "中", case["id"]
