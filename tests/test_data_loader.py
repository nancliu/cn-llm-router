"""数据层测试：加载完整性、覆盖率断言、缺价模型兼容（docs/spec/router-v1.md §2）。"""
import logging

from cn_llm_router.data_loader import COMPLEXITIES, load_data


def test_counts(data):
    assert len(data.models) >= 15
    assert len(data.categories) == 12
    # v1：36×15 格中有效分值（N/A/待补充不计数）。
    # 2026-10-05 登记火山方舟标准API双通道（CED-ARK/Seed-ARK 各复制基础行评分）后：
    # 375 + 36(CED-ARK 全有效) + 33(Seed-ARK 扣多模态 3 个空分格) = 444
    assert len(data.scores) == 444


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


def test_foreign_models_loaded(data):
    """ADR-0019：国内外对照模型加载（仅参考展示，不参与排序）。"""
    assert len(data.foreign_models) == 11
    foreign = [f for f in data.foreign_models if f.region == "国外"]
    domestic = [f for f in data.foreign_models if f.region == "国内"]
    assert len(foreign) == 5 and len(domestic) == 6
    # 有来源的 Elo 保留数值，待补充留 None（禁止编造）
    claude = next(f for f in foreign if "Claude" in f.model)
    assert claude.lmarena_elo == 1525
    astra = next(f for f in foreign if f.model == "GPT-6 Astra")
    assert astra.lmarena_elo is None
    # 综合成本口径 = 输入×0.6+输出×0.4（与 ADR-0001 一致）
    assert abs(claude.cost - 187.2) < 1e-9
    # 国内模型 Elo 全部留空（对照 Sheet 口径为待补充）
    assert all(f.lmarena_elo is None for f in domestic)
    # 来源可溯源
    assert all(f.source_url for f in data.foreign_models)
