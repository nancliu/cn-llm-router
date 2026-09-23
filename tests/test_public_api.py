"""公开 API 烟测：classify / select / route（docs/spec/router-v1.md §7 组合入口）。"""
import pytest

from cn_llm_router import classify, route, select
from cn_llm_router.config import RouterConfig
from cn_llm_router.types import Classification, Recommendation, RouteResult


@pytest.fixture
def offline_cfg(data, cfg):
    """关闭可用性过滤，离线可查全量推荐（数据文件齐全）。"""
    return RouterConfig(
        data_dir=cfg.data_dir,
        config_dir=cfg.config_dir,
        providers={},
        classifier_models=cfg.classifier_models,
        classifier_timeout=cfg.classifier_timeout,
        availability_filter=False,
        max_failover=cfg.max_failover,
    )


def test_select_offline(data, offline_cfg):
    rec = select("程序编码", "低", strategy="平衡", config=offline_cfg)
    assert isinstance(rec, Recommendation)
    assert rec.primary is not None
    assert rec.backup is not None
    assert rec.primary.logical_name != rec.backup.logical_name


def test_classify_offline(data, offline_cfg):
    clf = classify("帮我写一个Python函数解析JSON", config=offline_cfg)
    assert isinstance(clf, Classification)
    assert clf.category == "程序编码"
    assert clf.low_confidence is True  # 规则兜底


def test_route_offline(data, offline_cfg):
    res = route("写一个Python函数解析JSON", strategy="平衡", availability_filter=False, config=offline_cfg)
    assert isinstance(res, RouteResult)
    assert res.recommendation.primary is not None
    assert res.client is not None


def test_route_filter_default_no_key_raises(data, cfg):
    """route 默认开可用性过滤；无 key 时给出带提示的 ValueError。"""
    # 清空全部 key 环境变量
    import os

    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            os.environ.pop(k, None)
    with pytest.raises(ValueError) as ei:
        route("写一个Python函数解析JSON", strategy="平衡", config=cfg)
    assert "availability_filter" in str(ei.value)


def test_route_strategy_flow(data, cfg, monkeypatch):
    """strategy 参数透传到选择器（性价比优先的备选组合应可构造）。"""
    res = route("写一个Python函数解析JSON", strategy="性价比优先", availability_filter=False, config=cfg)
    assert res.recommendation.strategy == "性价比优先"
