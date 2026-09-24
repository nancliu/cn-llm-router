"""orchestrate() 编排层测试（ADR-0007）。

全部离线可跑：conftest 隔离 .env 后无 API key，classify 走规则关键词兜底；
select 是确定性函数，不依赖 LLM。offline_cfg 关闭可用性过滤以从全量集取推荐。
"""
import pytest

from cn_llm_router import OrchestrationResult, SubTaskSpec, orchestrate
from cn_llm_router.config import RouterConfig
from cn_llm_router.gateway import RouterClient
from cn_llm_router.types import Classification, Recommendation


@pytest.fixture
def offline_cfg(data, cfg):
    """providers 为空 + 关闭可用性过滤：离线可从全量集得到推荐。"""
    return RouterConfig(
        data_dir=cfg.data_dir,
        config_dir=cfg.config_dir,
        providers={},
        classifier_models=cfg.classifier_models,
        classifier_timeout=cfg.classifier_timeout,
        availability_filter=False,
        max_failover=cfg.max_failover,
    )


def test_empty_subtasks_raises():
    """空子任务列表抛 ValueError（在加载 config 之前）。"""
    with pytest.raises(ValueError, match="至少一个子任务"):
        orchestrate([])


def test_duplicate_id_raises():
    """子任务 id 重复抛 ValueError。"""
    specs = [
        SubTaskSpec(id="dup", description="写代码"),
        SubTaskSpec(id="dup", description="再写代码"),
    ]
    with pytest.raises(ValueError, match="重复"):
        orchestrate(specs)


def test_explicit_category_skips_classify(monkeypatch, offline_cfg):
    """显式给定 category+complexity 时不调用 classify，直接 select。"""
    from cn_llm_router import orchestrator as orch_mod

    called = []

    class SpyClassifier(orch_mod.Classifier):
        def classify(self, prompt):  # noqa: D401
            called.append(prompt)
            raise AssertionError("显式 category 不应触发 classify")

    monkeypatch.setattr(orch_mod, "Classifier", SpyClassifier)

    res = orchestrate(
        [SubTaskSpec(id="a", description="任意描述", category="程序编码", complexity="中")],
        availability_filter=False,
        config=offline_cfg,
    )
    assert called == []
    sub = res.subtasks[0]
    assert sub.classification.category == "程序编码"
    assert sub.classification.complexity == "中"
    assert sub.recommendation.primary is not None
    assert isinstance(sub.client, RouterClient)


def test_missing_category_uses_rule_fallback(offline_cfg):
    """缺 category 时走 classify；含 'python' 关键词稳定命中"程序编码"规则兜底。"""
    res = orchestrate(
        [SubTaskSpec(id="code", description="帮我写一个Python函数解析JSON")],
        availability_filter=False,
        config=offline_cfg,
    )
    clf = res.subtasks[0].classification
    assert clf.category == "程序编码"
    assert clf.fallback_reason == "规则关键词兜底"


def test_explicit_category_defaults_complexity(offline_cfg):
    """category 已给但 complexity 缺省时，复杂度取默认"中"。"""
    res = orchestrate(
        [SubTaskSpec(id="b", description="x", category="知识问答/检索")],
        availability_filter=False,
        config=offline_cfg,
    )
    sub = res.subtasks[0]
    assert sub.classification.category == "知识问答/检索"
    assert sub.classification.complexity == "中"


def test_multiple_subtasks_get_independent_clients(offline_cfg):
    """多子任务各自构建独立 RouterClient 对象。"""
    res = orchestrate(
        [
            SubTaskSpec(id="code", description="帮我写一个Python函数"),
            SubTaskSpec(id="summ", description="总结这篇长论文摘要"),
        ],
        availability_filter=False,
        config=offline_cfg,
    )
    assert len(res.subtasks) == 2
    c0 = res.subtasks[0].client
    c1 = res.subtasks[1].client
    assert isinstance(c0, RouterClient)
    assert isinstance(c1, RouterClient)
    assert c0 is not c1
    # 每个 client 绑定各自的 Recommendation
    assert c0.recommendation is res.subtasks[0].recommendation
    assert c1.recommendation is res.subtasks[1].recommendation


def test_subtask_strategy_overrides_global(offline_cfg):
    """子任务级 strategy 覆盖全局 strategy；结果级记录全局策略。"""
    res = orchestrate(
        [
            SubTaskSpec(id="cheap", description="x", category="程序编码", complexity="中", strategy="性价比优先"),
            SubTaskSpec(id="balanced", description="x", category="程序编码", complexity="中"),
        ],
        strategy="平衡",
        availability_filter=False,
        config=offline_cfg,
    )
    assert res.strategy == "平衡"
    by_id = {s.spec.id: s for s in res.subtasks}
    assert by_id["cheap"].recommendation.strategy == "性价比优先"
    assert by_id["balanced"].recommendation.strategy == "平衡"


def test_availability_filter_full_set_vs_empty(offline_cfg):
    """availability_filter=False 从全量集出推荐；True 在 providers 为空时无可选模型。"""
    ok = orchestrate(
        [SubTaskSpec(id="a", description="x", category="程序编码", complexity="中")],
        availability_filter=False,
        config=offline_cfg,
    )
    assert ok.availability_filtered is False
    assert ok.subtasks[0].recommendation.primary is not None

    # providers={} 且开启可用性过滤 → 无 key 可用 → select 抛 ValueError
    with pytest.raises(ValueError, match="无可选模型"):
        orchestrate(
            [SubTaskSpec(id="a", description="x", category="程序编码", complexity="中")],
            availability_filter=True,
            config=offline_cfg,
        )


def test_result_types(offline_cfg):
    """返回类型与公共导出对齐。"""
    res = orchestrate(
        [SubTaskSpec(id="a", description="x", category="程序编码", complexity="中")],
        availability_filter=False,
        config=offline_cfg,
    )
    assert isinstance(res, OrchestrationResult)
    sub = res.subtasks[0]
    assert isinstance(sub.classification, Classification)
    assert isinstance(sub.recommendation, Recommendation)
