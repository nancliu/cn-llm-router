"""serve 网关集成测试（ADR-0021）：fake 上游验证透明转发与路由模式。

覆盖：
- model=<logical_name> 点名透传：tools/messages/参数原样到达上游，model 由 api_model 覆盖
- 多轮 tool_call / tool_result 原样往返
- stream=True 流式逐 chunk 透传（[DONE] 结束）
- model=auto 判类路由（分类走 fake 上游 LLM 链）
- 未知模型 400 unknown_model / 点名缺 key 400 missing_key
- GET /v1/models 与 /health
- token 认证（401 / 放行）
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cn_llm_router.config import ProviderSpec, RouterConfig, load_config
from cn_llm_router.serve import AUTO_MODEL, create_app

FAKE_KEY = "FAKE_UPSTREAM_KEY"


class FakeUpstream:
    """本地 fake 上游：非流式返回固定 completion；流式返回 2 个 chunk + [DONE]。

    分类请求（system 为 JSON 分类器）返回合法分类 JSON，其余返回普通文本。
    """

    def __init__(self):
        self.received: list[dict] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def _make_handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.received.append(body)
                is_classify = any(
                    m.get("role") == "system"
                    and "JSON 分类器" in str(m.get("content", ""))
                    for m in body.get("messages", [])
                )
                stream = bool(body.get("stream", False))
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.end_headers()
                    for i in range(2):
                        chunk = {
                            "id": "chatcmpl-fake", "object": "chat.completion.chunk",
                            "created": 1, "model": body.get("model"),
                            "choices": [{"index": 0,
                                         "delta": {"content": f"part{i}"},
                                         "finish_reason": None if i == 0 else "stop"}],
                        }
                        self.wfile.write(
                            ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if is_classify:
                    content = json.dumps(
                        {"category": "程序编码", "complexity": "中",
                         "confidence": 0.9, "second_guess": ""},
                        ensure_ascii=False)
                else:
                    content = "hi"
                resp = {
                    "id": "chatcmpl-fake", "object": "chat.completion", "created": 1,
                    "model": body.get("model"),
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": content},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
                payload = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        return H

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def fake_upstream():
    up = FakeUpstream()
    yield up
    up.close()


@pytest.fixture()
def serve_cfg(fake_upstream, tmp_path_factory):
    """providers 指向 fake 上游（仅 Qwen 可用 + 一个缺 key 的 GLM），classifier 走 fake LLM。"""
    cfg = load_config(config_dir=str(tmp_path_factory.mktemp("srv")))
    os.environ[FAKE_KEY] = "dummy"
    cfg.providers = {
        "Qwen3.8-Max-0902": ProviderSpec(
            logical_name="Qwen3.8-Max-0902", provider="fake",
            base_url=f"http://127.0.0.1:{fake_upstream.port}/v1",
            api_model="qwen-fake", key_env=FAKE_KEY, timeout=10.0),
        "GLM-5.3": ProviderSpec(
            logical_name="GLM-5.3", provider="fake",
            base_url=f"http://127.0.0.1:{fake_upstream.port}/v1",
            api_model="glm-fake", key_env="GLM_UNSET_KEY", timeout=10.0),
    }
    cfg.classifier_models = ["Qwen3.8-Max-0902"]
    return cfg


@pytest.fixture()
def server(serve_cfg):
    handler_cls, cfg, data, classifier = create_app(config=serve_cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _post(url, body, headers=None, raw=False):
    req = urllib.request.Request(
        url + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            return resp.status, data if raw else json.loads(data or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(url, path, headers=None):
    req = urllib.request.Request(url + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------- 点名透传 ----------

def test_named_model_transparent_forward(server, fake_upstream):
    """model=logical_name：tools/messages/参数原样到达上游，响应透传。"""
    tools = [{"type": "function",
              "function": {"name": "read_file", "parameters": {"type": "object",
                                                               "properties": {"path": {"type": "string"}}}}}]
    messages = [
        {"role": "system", "content": "你是编码助手"},
        {"role": "user", "content": "实现一个函数"},
    ]
    status, resp = _post(server, {
        "model": "Qwen3.8-Max-0902",
        "messages": messages,
        "tools": tools,
        "temperature": 0.2,
        "max_tokens": 128,
    })
    assert status == 200
    assert resp["choices"][0]["message"]["content"] == "hi"
    # 上游收到的请求：model=api_model；messages/tools/temperature/max_tokens 原样
    got = fake_upstream.received[-1]
    assert got["model"] == "qwen-fake"
    assert got["messages"] == messages
    assert got["tools"] == tools
    assert got["temperature"] == 0.2
    assert got["max_tokens"] == 128


def test_tool_roundtrip_preserved(server, fake_upstream):
    """多轮 tool_call / tool_result 原样往返（harness 工具循环关键）。"""
    messages = [
        {"role": "system", "content": "你是编码助手"},
        {"role": "user", "content": "读取文件并总结"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "def f(): pass"},
        {"role": "user", "content": "继续"},
    ]
    status, _ = _post(server, {"model": "Qwen3.8-Max-0902", "messages": messages})
    assert status == 200
    got = fake_upstream.received[-1]
    assert got["messages"] == messages


def test_stream_forward_chunks(server, fake_upstream):
    """stream=True：SSE 逐 chunk 透传，[DONE] 结束，chunk 内容与 fake 一致。"""
    status, raw = _post(server, {
        "model": "Qwen3.8-Max-0902",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }, raw=True)
    assert status == 200
    text = raw.decode("utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("data: ")]
    assert lines[0].startswith("data: {")
    assert '"content": "part0"' in lines[0]
    assert '"content": "part1"' in lines[1]
    assert lines[2] == "data: [DONE]"


# ---------- auto 判类路由 ----------

def test_auto_routes_via_classifier(server, fake_upstream):
    """model=auto：分类走 LLM 链（fake 返回分类 JSON）→ select → 转发到可用模型。"""
    status, resp = _post(server, {
        "model": "auto",
        "messages": [{"role": "user", "content": "写一个 Python 函数解析 JSON"}],
    })
    assert status == 200
    assert resp["choices"][0]["message"]["content"] == "hi"
    got = fake_upstream.received[-1]
    assert got["model"] == "qwen-fake"  # 唯一可用模型
    # 分类请求也打到 fake（system=JSON 分类器）
    classify_req = fake_upstream.received[0]
    assert any("JSON 分类器" in str(m.get("content", "")) for m in classify_req["messages"])


def test_auto_default_model_omitted(server, fake_upstream):
    """model 缺省等价 auto。"""
    status, resp = _post(server, {
        "messages": [{"role": "user", "content": "写一个 Python 函数解析 JSON"}],
    })
    assert status == 200
    assert fake_upstream.received[-1]["model"] == "qwen-fake"


# ---------- 错误路径 ----------

def test_unknown_model_400(server):
    status, resp = _post(server, {
        "model": "不存在的模型",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 400
    assert resp["error"]["code"] == "unknown_model"


def test_missing_key_400(server):
    """点名模型在 providers 中但 key 未配置 → missing_key。"""
    status, resp = _post(server, {
        "model": "GLM-5.3",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 400
    assert resp["error"]["code"] == "missing_key"


def test_bad_json_400(server):
    req = urllib.request.Request(
        server + "/v1/chat/completions", data=b"{not-json",
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=15)
    assert ei.value.code == 400


# ---------- 元端点 ----------

def test_models_and_health(server):
    status, resp = _get(server, "/v1/models")
    assert status == 200
    ids = [m["id"] for m in resp["data"]]
    assert AUTO_MODEL in ids
    assert "Qwen3.8-Max-0902" in ids
    assert "GLM-5.3" not in ids  # 缺 key 不列出

    status, health = _get(server, "/health")
    assert status == 200
    assert health["status"] == "ok"


# ---------- 认证 ----------

def test_auth_token(serve_cfg):
    handler_cls, cfg, data, classifier = create_app(config=serve_cfg, token="secret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        status, resp = _post(url, {"model": "Qwen3.8-Max-0902",
                                   "messages": [{"role": "user", "content": "hi"}]})
        assert status == 401
        assert resp["error"]["code"] == "unauthorized"

        status, _ = _post(url, {"model": "Qwen3.8-Max-0902",
                                "messages": [{"role": "user", "content": "hi"}]},
                          headers={"Authorization": "Bearer secret"})
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
