"""统一网关（ADR-0003，docs/spec/router-v1.md §6）。

自建 OpenAI 兼容薄层：逻辑模型名 → {base_url, api_model, key_env} 映射；
RouterClient 请求指向主选，失败自动切备选（默认 1 次，可配/可关）；最终失败抛 RouteError。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from .config import ProviderSpec, RouterConfig
from .stats import UsageRecorder, prompt_hash
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
            t0 = time.monotonic()
            try:
                fn = client
                for part in method.split("."):
                    fn = getattr(fn, part)
                resp = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 —— 按类型判定是否可切
                last_err = e
                if not self._is_retryable(e) or choice is chain[-1]:
                    break
                logger.info("模型 %s 调用失败，切备选: %s", choice.logical_name, e)
                continue
            duration_ms = (time.monotonic() - t0) * 1000
            self._record_completion(choice.logical_name, resp, kwargs, duration_ms)
            return resp
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

    def _record_completion(self, model_name: str, resp: object, kwargs: dict, duration_ms: float) -> None:
        """成功调用后记录真实用量（ADR-0011 completion 事件）；关闭或无 usage 时空操作。"""
        if not getattr(self.cfg, "stats_enabled", False):
            return
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None and completion_tokens is None:
            return
        # 惰性加载一次模型单价（仅开启统计时付出成本）
        prices: tuple[Optional[float], Optional[float]] = (None, None)
        try:
            if not hasattr(self, "_stats_models"):
                from .data_loader import load_data

                self._stats_models = load_data(self.cfg.data_dir).models  # type: ignore[attr-defined]
            spec = self._stats_models.get(model_name)  # type: ignore[attr-defined]
            if spec is not None:
                prices = (spec.price_in, spec.price_out)
        except Exception as e:  # noqa: BLE001 —— 统计旁路失败不影响调用
            logger.debug("completion 统计取价失败（忽略）: %s", e)
        # 从 messages 取最后一条用户文本做 hash（不记录内容）
        last_user = ""
        for m in reversed(kwargs.get("messages") or []):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                last_user = m["content"]
                break
        UsageRecorder(enabled=True, log_path=self.cfg.stats_log_path).record({
            "event_type": "completion",
            "prompt_hash": prompt_hash(last_user) if last_user else "",
            "prompt_chars": len(last_user),
            "category": "",  # completion 事件不回填类别，留空
            "complexity": "",
            "strategy": self.recommendation.strategy,
            "classifier_model": "",
            "primary_model": model_name,
            "backup_model": "",
            "duration_ms": round(duration_ms, 2),
            "estimated_input_tokens": prompt_tokens or 0,
            "estimated_output_tokens": completion_tokens or 0,
            "estimated_cost_yuan": UsageRecorder.estimate_cost(
                prompt_tokens or 0, completion_tokens or 0, prices[0], prices[1]
            ),
            "cache_hit": False,
        })

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
