import pytest

from cn_llm_router import load_config, load_data


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def data(cfg):
    return load_data(cfg.data_dir)


def load_golden_cases():
    from pathlib import Path

    import yaml

    p = Path(__file__).parent / "fixtures" / "golden_cases.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    return doc["cases"]
