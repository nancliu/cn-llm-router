"""配置加载（docs/spec/router-v1.md §8）。

查找顺序：显式参数 > CN_LLM_ROUTER_CONFIG 目录 > 仓库默认（data/ + config/）。
零配置 clone 即用：classify/select 离线可用；route 需配置 API key（.env.example 模板）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = PACKAGE_ROOT / "config"
ENV_CONFIG_DIR = "CN_LLM_ROUTER_CONFIG"
ENV_PREFIX = "CN_LLM_ROUTER_"


def default_data_dir() -> Path:
    """默认数据目录解析（ADR-0004 唯一真相源 data/*.csv）。

    顺序：仓库根 data/（开发/源码模式）→ 随 wheel 安装的 data 包（site-packages/data）。
    """
    repo_data = PACKAGE_ROOT / "data"
    if (repo_data / "VERSION").exists():
        return repo_data
    try:
        import data as _data_pkg  # noqa: PLC0415 —— wheel 分发为顶层包
        return Path(_data_pkg.__file__).resolve().parent
    except ImportError:
        return repo_data


DEFAULT_DATA_DIR = default_data_dir()


@dataclass
class ProviderSpec:
    """逻辑模型名 → 上游厂商接入信息（ADR-0003）。

    backend: "openai"（默认，OpenAI 兼容端点）| "litellm"（经 LiteLLM 直连，需 cn-llm-router[litellm]）。
    """

    logical_name: str
    provider: str
    base_url: str
    api_model: str
    key_env: str
    timeout: float = 60.0
    backend: str = "openai"


@dataclass
class RouterConfig:
    data_dir: Path = DEFAULT_DATA_DIR
    config_dir: Path = DEFAULT_CONFIG_DIR
    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    classifier_models: list[str] = field(default_factory=lambda: ["GLM-5.3-Flash", "DeepSeek-V4-Flash-0731"])
    classifier_timeout: float = 30.0
    availability_filter: bool = True
    max_failover: int = 1
    weights_override: Optional[dict] = None  # {category: {dimension: weight}} 或 None


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def resolve_dirs(config_dir: Optional[str]) -> tuple[Path, Path]:
    """解析 data_dir 与 config_dir。"""
    data_dir = DEFAULT_DATA_DIR
    cdir = Path(config_dir) if config_dir else None
    if cdir is None and os.environ.get(ENV_CONFIG_DIR):
        cdir = Path(os.environ[ENV_CONFIG_DIR])
    if cdir is not None:
        # 用户配置目录优先；data 仍用仓库默认（v1 不支持数据目录覆盖）
        data_dir = DEFAULT_DATA_DIR
    return data_dir, cdir or DEFAULT_CONFIG_DIR


def load_config(config_dir: Optional[str] = None) -> RouterConfig:
    """加载配置：providers.yaml（缺省用 providers.example.yaml）、classifier.yaml、weights.yaml 覆盖。"""
    data_dir, cdir = resolve_dirs(config_dir)
    cfg = RouterConfig(data_dir=data_dir, config_dir=cdir)

    # providers：config/providers.yaml 缺省回退到 providers.example.yaml
    pfile = cdir / "providers.yaml"
    if not pfile.exists():
        pfile = DEFAULT_CONFIG_DIR / "providers.example.yaml"
    prow = _read_yaml(pfile)
    providers: dict[str, ProviderSpec] = {}
    for logical, spec in prow.get("providers", {}).items():
        key_env = str(spec.get("key_env", f"{ENV_PREFIX}{logical.upper().replace('-', '_')}_KEY"))
        base_url = str(spec.get("base_url", ""))
        # 环境变量覆盖 base_url：{KEY_ENV 去 _API_KEY 后缀}_BASE_URL（如 ZHIPU_BASE_URL）
        # 用于自建网关/中转端点；未配置时回退 yaml 默认值。
        if key_env.endswith("_API_KEY"):
            env_base = os.environ.get(key_env[: -len("_API_KEY")] + "_BASE_URL")
            if env_base:
                base_url = env_base
        providers[logical] = ProviderSpec(
            logical_name=logical,
            provider=str(spec.get("provider", "")),
            base_url=base_url,
            api_model=str(spec.get("api_model", logical)),
            key_env=key_env,
            timeout=float(spec.get("timeout", 60.0)),
            backend=str(spec.get("backend", "openai")),
        )
    cfg.providers = providers

    # classifier：候选模型链
    cfile = cdir / "classifier.yaml"
    if cfile.exists():
        crow = _read_yaml(cfile)
        if crow.get("models"):
            cfg.classifier_models = [str(m) for m in crow["models"]]
        cfg.classifier_timeout = float(crow.get("timeout", cfg.classifier_timeout))

    # selector 覆盖
    sfile = cdir / "selector.yaml"
    if sfile.exists():
        srow = _read_yaml(sfile)
        if "availability_filter" in srow:
            cfg.availability_filter = bool(srow["availability_filter"])
        if "max_failover" in srow:
            cfg.max_failover = int(srow["max_failover"])

    # weights 覆盖（ADR 词汇表：策略档位；v1 中作为口径覆盖入口，见 data_loader 说明）
    wfile = cdir / "weights.yaml"
    if wfile.exists():
        wrow = _read_yaml(wfile)
        cfg.weights_override = wrow.get("weights")

    return cfg


def key_available(provider: ProviderSpec, environ: Optional[dict] = None) -> bool:
    """供应商可用性（ADR-0005）：对应 key 环境变量已配置（非空）。"""
    env = os.environ if environ is None else environ
    return bool(str(env.get(provider.key_env, "")).strip())
