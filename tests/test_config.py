"""config 加载测试：_BASE_URL 环境变量覆盖 base_url（隔离本地 providers.yaml 影响）。"""
import pytest

from cn_llm_router import load_config


def _cfg(tmp_path):
    """临时空 config_dir：providers 回退到 providers.example.yaml，隔离本地配置。"""
    return load_config(config_dir=str(tmp_path))


def test_base_url_env_override(monkeypatch, tmp_path):
    """ZHIPU_BASE_URL 存在时覆盖 providers.example.yaml 的默认 base_url。"""
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://my-gateway.example.com/v1")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    cfg = _cfg(tmp_path)
    glm = cfg.providers["GLM-5.3-Flash"]
    assert glm.base_url == "https://my-gateway.example.com/v1"
    assert glm.key_env == "ZHIPU_API_KEY"


def test_base_url_env_not_set_falls_back(monkeypatch, tmp_path):
    """未配置 _BASE_URL 时回退 yaml 默认值。"""
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    cfg = _cfg(tmp_path)
    ds = cfg.providers["DeepSeek-V4-Flash-0731"]
    assert ds.base_url == "https://api.deepseek.com"


def test_base_url_env_empty_ignored(monkeypatch, tmp_path):
    """_BASE_URL 为空串时不覆盖（等价于未配置）。"""
    monkeypatch.setenv("VOLCENGINE_BASE_URL", "")
    cfg = _cfg(tmp_path)
    vc = cfg.providers["豆包Seed-2.1-Pro"]
    assert vc.base_url == "https://ark.cn-beijing.volces.com/api/v3"
