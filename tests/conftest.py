import os

import pytest

from cn_llm_router import load_config, load_data


@pytest.fixture(scope="session", autouse=True)
def _isolate_local_env():
    """隔离本地用户配置（.env / config/providers.yaml / config/classifier.yaml）：
    测试进程禁用 .env 自动加载，保证 classify 走确定性规则兜底（不真实调 LLM）。
    """
    saved = os.environ.get("CN_LLM_ROUTER_NO_DOTENV")
    os.environ["CN_LLM_ROUTER_NO_DOTENV"] = "1"
    yield
    if saved is None:
        os.environ.pop("CN_LLM_ROUTER_NO_DOTENV", None)
    else:
        os.environ["CN_LLM_ROUTER_NO_DOTENV"] = saved


@pytest.fixture(scope="session")
def cfg(tmp_path_factory):
    # 临时空 config_dir：providers 回退到 example（官方端点）、无 classifier.yaml 覆盖
    return load_config(config_dir=str(tmp_path_factory.mktemp("cfg")))


@pytest.fixture(scope="session")
def data(cfg):
    return load_data(cfg.data_dir)


def load_golden_cases():
    from pathlib import Path

    import yaml

    p = Path(__file__).parent / "fixtures" / "golden_cases.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    return doc["cases"]
