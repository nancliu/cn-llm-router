"""统一网关（ADR-0003，docs/spec/router-v1.md §6）。

自建 OpenAI 兼容薄层：逻辑模型名 → {base_url, api_model, key_env} 映射；
RouterClient 请求指向主选，失败自动切备选（默认 1 次，可配/可关）；最终失败抛 RouteError。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .config import ProviderSpec, RouterConfig
from .types import Recommendation, RouteError

logger = logging.getLogger("cn_llm_router.gateway")

# 失败切备选适用的异常类型（网络/5xx/429/超时；鉴权失败不重试）
_RETRYABLE = ("APIConnectionError", "APITimeoutError", "InternalServerError", "RateLimitError")


def _client_kwargs(prov: ProviderSpec) -> dict:
    _require_key(prov)
    return {"base_url": prov.base_url, "api_key": os.environ.get(prov.key_env, ""), "timeout": prov.timeout}


def _require_key(prov: ProviderSpec) -> str:
    key = os.environ.get(prov.key_env, "")
    if not key:
        raise RouteError(
            f"模型 {prov.logical_name} 的 API key 未配置（环境变量 {prov.key_env}）",
            cause="missing_key",
            attempted=[prov.logical_name],
        )
    return key


def build_client(prov: ProviderSpec):
    """构建指向该 provider 的客户端：backend=openai（默认）| litellm（ADR-0003 预留接口落地）。"""
    if getattr(prov, "backend", "openai") == "litellm":
        _require_key(prov)
        try:
            import litellm  # noqa: F401 —— 懒加载校验依赖
        except ImportError:
            raise RouteError(
                f"模型 {prov.logical_name} 使用 backend=litellm，但未安装 litellm：pip install 'cn-llm-router[litellm]'",
                cause="litellm_missing",
                attempted=[prov.logical_name],
            ) from None
        return LiteLLMClient(prov)

    from openai import OpenAI

    return OpenAI(**_client_kwargs(prov))


class _LiteLLMCompletions:
    """OpenAI 兼容 completions：经 litellm.completion 直连上游（provider/api_model 组合路由）。"""

    def __init__(self, prov: ProviderSpec):
        self.prov = prov

    def create(self, *args, **kwargs):
        import litellm

        kwargs = dict(kwargs)
        api_model = kwargs.pop("model", self.prov.api_model)
        model = f"{self.prov.provider}/{api_model}" if self.prov.provider else api_model
        params = {"model": model}
        key = os.environ.get(self.prov.key_env, "") or None
        if key:
            params["api_key"] = key
        if self.prov.base_url:
            params["api_base"] = self.prov.base_url
        if self.prov.timeout:
            params.setdefault("timeout", self.prov.timeout)
        params.update(kwargs)
        return litellm.completion(**params)


class _LiteLLMChat:
    def __init__(self, prov: ProviderSpec):
        self.prov = prov
        self.completions = _LiteLLMCompletions(prov)


class LiteLLMClient:
    """backend=litellm 的客户端外壳（.chat.completions.create 与 OpenAI 兼容）。"""

    def __init__(self, prov: ProviderSpec):
        self.prov = prov
        self.chat = _LiteLLMChat(prov)


class _CompletionsProxy:
    """OpenAI 兼容 completions 代理：主选失败自动切备选。"""

    def __init__(self, owner: "RouterClient"):
        self._owner = owner

    def create(self, *args, **kwargs):
        return self._owner._call("chat.completions.create", args, kwargs)


class _ChatProxy:
    def __init__(self, owner: "RouterClient"):
        self._owner = owner
        self.completions = _CompletionsProxy(owner)


class RouterClient:
    """推荐结果附带的 OpenAI 兼容客户端（懒加载，失败切备选）。"""

    def __init__(self, recommendation: Recommendation, cfg: RouterConfig, allow_failover: bool = True):
        self.recommendation = recommendation
        self.cfg = cfg
        self.allow_failover = allow_failover
        self.max_failover = max(0, cfg.max_failover)
        self._primary: Optional[object] = None
        self._backup: Optional[object] = None
        self.attempted: list[str] = []
        self.chat = _ChatProxy(self)

    def _call(self, method: str, args: tuple, kwargs: dict):
        chain = [self.recommendation.primary]
        if self.allow_failover and self.recommendation.backup and self.max_failover > 0:
            chain.append(self.recommendation.backup)
        last_err: Optional[Exception] = None
        for choice in chain:
            self.attempted.append(choice.logical_name)
            client = self._client_for(choice.logical_name)
            try:
                fn = client
                for part in method.split("."):
                    fn = getattr(fn, part)
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 —— 按类型判定是否可切
                last_err = e
                if not self._is_retryable(e) or choice is chain[-1]:
                    break
                logger.info("模型 %s 调用失败，切备选: %s", choice.logical_name, e)
        raise RouteError(
            "上游模型调用最终失败",
            cause=f"{type(last_err).__name__}: {last_err}" if last_err else "unknown",
            attempted=self.attempted,
        )

    def _client_for(self, logical: str):
        prov = self.cfg.providers.get(logical)
        if prov is None:
            raise RouteError(f"模型 {logical} 未配置 provider（config/providers.yaml）", cause="no_provider", attempted=[logical])
        return build_client(prov)

    @staticmethod
    def _is_retryable(e: Exception) -> bool:
        name = type(e).__name__
        if any(r in name for r in _RETRYABLE):
            return True
        # 网络层/超时/5xx/429（openai 异常族）按消息特征兜底；鉴权(401)不重试
        msg = str(e)
        return any(k in msg for k in ("Connection", "timeout", "timed out", "429", "502", "503", "504"))


def get_client(recommendation: Recommendation, cfg: RouterConfig, allow_failover: bool = True) -> RouterClient:
    """route() 结果获取 OpenAI 兼容客户端的便捷入口。"""
    return RouterClient(recommendation, cfg, allow_failover=allow_failover)
