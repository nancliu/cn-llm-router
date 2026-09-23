"""数据层测试：加载完整性、覆盖率断言、缺价模型兼容（docs/spec/router-v1.md §2）。"""
import logging

from cn_llm_router.data_loader import COMPLEXITIES, load_data


def test_counts(data):
    assert len(data.models) >= 15
    assert len(data.categories) == 12
    assert len(data.scores) == 375  # v1：36×15 格中有效分值（N/A/待补充不计数）


def test_version(data):
    assert data.version == "v1-20260923"


def test_complexity_coverage(data):
    for cat in data.categories:
        levels = {k[1] for k in data.scores if k[0] == cat}
        assert levels == set(COMPLEXITIES), cat


def test_weights_coverage(data):
    for cat in data.categories:
        dims = [k[1] for k in data.weights if k[0] == cat]
        assert len(dims) == 8, cat
    # 多模态理解：直接用VLM分 备注保留
    notes = {k: v for k, v in data.weight_notes.items() if k[0] == "多模态理解"}
    assert len(notes) == 8
    assert "直接用VLM分" in next(iter(notes.values()))


def test_missing_price_model_allowed(data, caplog):
    """Seedream 无价目：纯能力档可参与（+inf），其余档被排除；加载不报错。"""
    seedream = data.models.get("Seedream-5.0")
    assert seedream is not None
    assert seedream.cost is None
    # Seedream 文本任务全 N/A：无有效分，任何策略都不应选中它
    assert all(k[2] != "Seedream-5.0" for k in data.scores)


def test_duplicate_keys_none(data):
    assert len({(k[0], k[1], k[2]) for k in data.scores}) == len(data.scores)
    assert len({(k[0], k[1]) for k in data.weights}) == len(data.weights)
