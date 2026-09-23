"""config 加载测试：_BASE_URL 环境变量覆盖 base_url、.env 自动加载（隔离本地配置影响）。"""
import os
from pathlib import Path

import pytest

from cn_llm_router import load_config
from cn_llm_router.config import _load_dotenv


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


def test_dotenv_loads_keys(monkeypatch, tmp_path):
    """_load_dotenv：注入键值、去引号、不覆盖已存在的环境变量。"""
    for k in ("ZHIPU_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "VOLCENGINE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "already-set")
    env_file = tmp_path / ".env"
    env_file.write_text(
        'ZHIPU_API_KEY=should-not-override\n'
        'DEEPSEEK_API_KEY="sk-quoted"\n'
        'MOONSHOT_API_KEY=\n'
        '# 注释行\n'
        'VOLCENGINE_API_KEY=ark-plain\n',
        encoding="utf-8",
    )
    _load_dotenv(env_file)
    assert os.environ["ZHIPU_API_KEY"] == "already-set"  # 已有值不被覆盖
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-quoted"  # 引号被去除
    assert os.environ["VOLCENGINE_API_KEY"] == "ark-plain"
    assert os.environ.get("MOONSHOT_API_KEY", "") == ""  # 空值不注入


def test_dotenv_missing_noop(monkeypatch, tmp_path):
    """_load_dotenv：文件不存在时是空操作。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    _load_dotenv(tmp_path / "no-such.env")
    assert "ZHIPU_API_KEY" not in os.environ


def test_dotenv_disabled_by_env(monkeypatch, tmp_path):
    """CN_LLM_ROUTER_NO_DOTENV=1 时禁用 .env 加载（测试/CI 隔离）。"""
    monkeypatch.setenv("CN_LLM_ROUTER_NO_DOTENV", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-x\n", encoding="utf-8")
    _load_dotenv(env_file)
    assert "DEEPSEEK_API_KEY" not in os.environ

