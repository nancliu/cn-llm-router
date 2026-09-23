"""网关测试：失败切备选、非重试错误直抛、可关 failover（ADR-0003）。"""
import pytest

from cn_llm_router.config import RouterConfig, ProviderSpec
from cn_llm_router.gateway import RouterClient
from cn_llm_router.types import ModelChoice, Recommendation, RouteError


def _rec(primary_name="A", backup_name="B", primary_vendor="v1", backup_vendor="v2"):
    primary = ModelChoice(logical_name=primary_name, vendor=primary_vendor, score=70.0, cost=5.0, rank=1, reason="x")
    backup = ModelChoice(logical_name=backup_name, vendor=backup_vendor, score=65.0, cost=5.0, rank=2, reason="x")
    return Recommendation(strategy="平衡", availability_filtered=False, primary=primary, backup=backup)


def _cfg():
    return RouterConfig(
        providers={
            "A": ProviderSpec("A", "p1", "http://a", "a", "K1"),
            "B": ProviderSpec("B", "p2", "http://b", "b", "K2"),
        }
    )


class _Completions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, *a, **kw):
        if self.owner.fail:
            raise self.owner.fail
        return {"ok": True, "model": self.owner.logical}


class _Chat:
    def __init__(self, owner):
        self.owner = owner  # owner = 客户端实例
        self.completions = _Completions(self.owner)


class _FakeClient:
    """按模型名决定行为：可注入 fail 异常。"""

    def __init__(self, logical, fail=None):
        self.logical = logical
        self.fail = fail
        self.chat = _Chat(self)


class _ConnErr(Exception):
    pass


class _AuthErr(Exception):
    pass


def test_failover_to_backup(monkeypatch):
    import cn_llm_router.gateway as g

    def fake(prov):
        fail = _ConnErr("Connection reset") if prov.logical_name == "A" else None
        return _FakeClient(prov.logical_name, fail=fail)

    monkeypatch.setattr(g, "build_client", fake)
    client = RouterClient(_rec(), _cfg())
    out = client.chat.completions.create(messages=[])
    assert out["model"] == "B"
    assert client.attempted == ["A", "B"]


def test_success_on_primary(monkeypatch):
    import cn_llm_router.gateway as g

    monkeypatch.setattr(g, "build_client", lambda prov: _FakeClient(prov.logical_name))
    client = RouterClient(_rec(), _cfg())
    assert client.chat.completions.create(messages=[])["model"] == "A"
    assert client.attempted == ["A"]


def test_non_retryable_raises_immediately(monkeypatch):
    import cn_llm_router.gateway as g

    monkeypatch.setattr(g, "build_client", lambda prov: _FakeClient(prov.logical_name, fail=_AuthErr("401 invalid")))
    client = RouterClient(_rec(), _cfg())
    with pytest.raises(RouteError) as ei:
        client.chat.completions.create(messages=[])
    assert ei.value.attempted == ["A"]  # 鉴权失败不切备选
    assert "401" in ei.value.cause


def test_failover_disabled(monkeypatch):
    import cn_llm_router.gateway as g

    monkeypatch.setattr(g, "build_client", lambda prov: _FakeClient(prov.logical_name, fail=_ConnErr("timeout")))
    client = RouterClient(_rec(), _cfg(), allow_failover=False)
    with pytest.raises(RouteError) as ei:
        client.chat.completions.create(messages=[])
    assert ei.value.attempted == ["A"]


def test_no_backup_fails_after_primary(monkeypatch):
    import cn_llm_router.gateway as g

    rec = _rec(backup_name=None)
    rec.backup = None
    monkeypatch.setattr(g, "build_client", lambda prov: _FakeClient(prov.logical_name, fail=_ConnErr("timeout")))
    client = RouterClient(rec, _cfg())
    with pytest.raises(RouteError) as ei:
        client.chat.completions.create(messages=[])
    assert ei.value.attempted == ["A"]
    assert "最终失败" in ei.value.message


def test_missing_key_env_raises(monkeypatch):
    import cn_llm_router.gateway as g

    monkeypatch.setattr(g, "build_client", g.build_client)  # 原实现：读环境变量
    client = RouterClient(_rec(), _cfg())
    with pytest.raises(RouteError) as ei:
        client.chat.completions.create(messages=[])
    assert "API key 未配置" in ei.value.message


# ---- backend=litellm（ADR-0003 预留接口落地） ----

def _litellm_prov():
    from cn_llm_router.config import ProviderSpec

    return ProviderSpec("GLM-5.3-Flash", "zhipu", "https://open.bigmodel.cn/api/paas/v4",
                        "glm-5.3-flash", "ZHIPU_API_KEY", backend="litellm")


def test_litellm_backend_routes_to_litellm(monkeypatch):
    import sys
    import types

    import cn_llm_router.gateway as g

    calls = {}
    fake_litellm = types.ModuleType("litellm")

    def fake_completion(**kwargs):
        calls.update(kwargs)
        return {"model": kwargs["model"], "choices": [{"message": {"content": "ok"}}]}

    fake_litellm.completion = fake_completion
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")

    client = g.build_client(_litellm_prov())
    out = client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])
    assert out["model"] == "zhipu/glm-5.3-flash"
    assert calls["api_key"] == "test-key"
    assert calls["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
    assert calls["messages"] == [{"role": "user", "content": "hi"}]


def test_litellm_backend_provider_prefix_optional(monkeypatch):
    import sys
    import types

    import cn_llm_router.gateway as g
    from cn_llm_router.config import ProviderSpec

    calls = {}
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = lambda **kw: calls.update(kw) or {"model": kw["model"]}
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setenv("K1", "k")
    prov = ProviderSpec("A", "", "", "qwen", "K1", backend="litellm")  # 无 provider 前缀
    client = g.build_client(prov)
    assert client.chat.completions.create(messages=[])["model"] == "qwen"


def test_litellm_missing_dep_raises(monkeypatch):
    import builtins

    import cn_llm_router.gateway as g

    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    orig_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "litellm":
            raise ImportError("No module named 'litellm'")
        return orig_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RouteError) as ei:
        g.build_client(_litellm_prov())
    assert "litellm" in ei.value.cause


def test_litellm_failover_via_router(monkeypatch):
    """RouterClient 全链路：主选 litellm 失败 → 切备选 openai。"""
    import sys
    import types

    import cn_llm_router.gateway as g

    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = lambda **kw: (_ for _ in ()).throw(_ConnErr("Connection reset"))
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setenv("K1", "k1")
    monkeypatch.setenv("K2", "k2")

    def fake_build(prov):
        if getattr(prov, "backend", "openai") == "litellm":
            return g.LiteLLMClient(prov)
        return _FakeClient(prov.logical_name, fail=None)

    monkeypatch.setattr(g, "build_client", fake_build)
    rec = _rec(primary_name="A", backup_name="B")
    cfg = RouterConfig(
        providers={
            "A": g.ProviderSpec("A", "p1", "http://a", "a", "K1", backend="litellm"),
            "B": g.ProviderSpec("B", "p2", "http://b", "b", "K2"),
        }
    )
    client = RouterClient(rec, cfg)
    out = client.chat.completions.create(messages=[])
    assert out["model"] == "B"
    assert client.attempted == ["A", "B"]
