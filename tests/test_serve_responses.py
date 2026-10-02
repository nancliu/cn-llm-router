"""serve /v1/responses 端点测试（ADR-0021，LiteLLM 桥接上游）。

覆盖：
- 非流式：input(string/items) + tools → Responses JSON（output message / function_call）
- function_call / function_call_output 输入项 → chat messages 原样往返
- 流式：SSE 事件序列（response.created → output_text.delta → response.completed → [DONE]）
- 错误：unknown_model → 400 error JSON
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cn_llm_router.config import ProviderSpec, RouterConfig, load_config
from cn_llm_router.serve import AUTO_MODEL, _extract_classify_text, create_app

FAKE_KEY = "FAKE_UPSTREAM_KEY"


class FakeUpstream:
    """本地 fake 上游：分类请求返回分类 JSON；其余按 tool_calls 模式返回工具调用或文本。"""

    def __init__(self, tool_calls=True):
        self.received: list[dict] = []
        self.tool_calls = tool_calls
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
                if is_classify:
                    content = json.dumps(
                        {"category": "程序编码", "complexity": "中",
                         "confidence": 0.9, "second_guess": ""},
                        ensure_ascii=False)
                    message = {"role": "assistant", "content": content,
                               "tool_calls": None}
                elif outer.tool_calls:
                    message = {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "call_abc123", "type": "function",
                            "function": {"name": "get_weather",
                                         "arguments": '{"city": "北京"}'},
                        }],
                    }
                else:
                    message = {"role": "assistant", "content": "hi", "tool_calls": None}
                resp = {
                    "id": "chatcmpl-fake", "object": "chat.completion", "created": 1,
                    "model": body.get("model"),
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
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
    up = FakeUpstream(tool_calls=True)
    yield up
    up.close()


@pytest.fixture()
def fake_upstream_text():
    up = FakeUpstream(tool_calls=False)
    yield up
    up.close()


@pytest.fixture()
def server(fake_upstream, tmp_path_factory):
    cfg = load_config(config_dir=str(tmp_path_factory.mktemp("srv")))
    os.environ[FAKE_KEY] = "dummy"
    cfg.providers = {
        "Qwen3.8-Max-0902": ProviderSpec(
            logical_name="Qwen3.8-Max-0902", provider="fake",
            base_url=f"http://127.0.0.1:{fake_upstream.port}/v1",
            api_model="qwen-fake", key_env=FAKE_KEY, timeout=10.0),
    }
    cfg.classifier_models = ["Qwen3.8-Max-0902"]
    handler_cls, cfg2, data, classifier = create_app(config=cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", fake_upstream
    httpd.shutdown()
    httpd.server_close()


def _post(url, body, headers=None, raw=False):
    req = urllib.request.Request(
        url + "/v1/responses", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            return resp.status, data if raw else json.loads(data or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_responses_non_stream_text(fake_upstream_text, tmp_path_factory):
    """非流式：input 字符串 + tools → Responses JSON（output message）。"""
    cfg = load_config(config_dir=str(tmp_path_factory.mktemp("srv_txt2")))
    os.environ[FAKE_KEY] = "dummy"
    cfg.providers = {
        "Qwen3.8-Max-0902": ProviderSpec(
            logical_name="Qwen3.8-Max-0902", provider="fake",
            base_url=f"http://127.0.0.1:{fake_upstream_text.port}/v1",
            api_model="qwen-fake", key_env=FAKE_KEY, timeout=10.0),
    }
    cfg.classifier_models = ["Qwen3.8-Max-0902"]
    handler_cls, cfg2, data, classifier = create_app(config=cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        status, resp = _post(url, {
            "model": "auto",
            "input": "写一个 Python 函数",
            "instructions": "你是编码助手",
            "tools": [],
        })
        assert status == 200
        assert resp["object"] == "response"
        assert resp["status"] == "completed"
        assert resp["output"][0]["type"] == "message"
        assert resp["output"][0]["content"][0]["type"] == "output_text"
        assert resp["output"][0]["content"][0]["text"] == "hi"
        assert resp["output_text"] == "hi"
        assert resp["usage"]["input_tokens"] == 10
        # 上游收到 chat 格式（instructions → system）
        got = fake_upstream_text.received[-1]
        assert got["messages"][0] == {"role": "system", "content": "你是编码助手"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_responses_tool_calls_output(server):
    """上游返回 tool_calls → Responses function_call item。"""
    url, _ = server
    status, resp = _post(url, {
        "model": "auto",
        "input": "查询北京的天气",
        "tools": [{"type": "function", "name": "get_weather",
                   "description": "查询天气",
                   "parameters": {"type": "object",
                                  "properties": {"city": {"type": "string"}},
                                  "required": ["city"]}}],
    })
    assert status == 200
    fc = [o for o in resp["output"] if o["type"] == "function_call"]
    assert fc, resp
    assert fc[0]["name"] == "get_weather"
    assert fc[0]["call_id"] == "call_abc123"
    assert "北京" in fc[0]["arguments"]


def test_responses_input_items_roundtrip(server):
    """input items 里的 function_call / function_call_output → chat messages 原样透传。"""
    url, up = server
    call_id = "call_abc123"
    status, _ = _post(url, {
        "model": "Qwen3.8-Max-0902",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "读取文件"}]},
            {"type": "function_call", "call_id": call_id, "name": "read_file",
             "arguments": '{"path": "a.py"}'},
            {"type": "function_call_output", "call_id": call_id, "output": "def f(): pass"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "总结"}]},
        ],
    })
    assert status == 200
    got = up.received[-1]
    assert got["messages"][0] == {"role": "user",
                                  "content": [{"type": "text", "text": "读取文件"}]}
    assert got["messages"][1] == {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
    }
    assert got["messages"][2] == {"role": "tool", "tool_call_id": call_id,
                                  "content": "def f(): pass"}
    assert got["messages"][3] == {"role": "user",
                                  "content": [{"type": "text", "text": "总结"}]}


def test_responses_stream_events(server):
    """流式（工具调用输出）：SSE 事件序列含 created / function_call added / completed / [DONE]。"""
    url, _ = server
    status, raw = _post(url, {
        "model": "auto",
        "input": "写一个 Python 函数",
        "stream": True,
    }, raw=True)
    assert status == 200
    text = raw.decode("utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines if ln != "data: [DONE]"]
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert "response.output_item.added" in types
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"
    assert lines[-1] == "data: [DONE]"
    # 工具调用输出：added 的 item 为 function_call（无文本 delta）
    added = [e for e in events if e["type"] == "response.output_item.added"]
    assert added and added[0]["item"]["type"] == "function_call"
    assert added[0]["item"]["name"] == "get_weather"


def test_responses_stream_text_events(fake_upstream_text, tmp_path_factory):
    """流式 + 文本输出：delta 事件携带上游文本。"""
    cfg = load_config(config_dir=str(tmp_path_factory.mktemp("srv_txt")))
    os.environ[FAKE_KEY] = "dummy"
    cfg.providers = {
        "Qwen3.8-Max-0902": ProviderSpec(
            logical_name="Qwen3.8-Max-0902", provider="fake",
            base_url=f"http://127.0.0.1:{fake_upstream_text.port}/v1",
            api_model="qwen-fake", key_env=FAKE_KEY, timeout=10.0),
    }
    cfg.classifier_models = ["Qwen3.8-Max-0902"]
    handler_cls, cfg2, data, classifier = create_app(config=cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        status, raw = _post(url, {
            "model": "auto",
            "input": "写一个 Python 函数",
            "stream": True,
        }, raw=True)
        assert status == 200
        lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.startswith("data: ")]
        events = [json.loads(ln[6:]) for ln in lines if ln != "data: [DONE]"]
        deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
        assert deltas and deltas[-1] == "hi"
        assert lines[-1] == "data: [DONE]"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_responses_unknown_model(server):
    url, _ = server
    status, resp = _post(url, {
        "model": "不存在的模型",
        "input": "hi",
    })
    assert status == 400
    assert resp["error"]["code"] == "unknown_model"


def test_responses_merge_consecutive_assistant(fake_upstream, tmp_path_factory):
    """litellm 把 tool_use 拆成独立 function_call item：tool_calls assistant 与文本 assistant
    必须合并，且 tool 消息紧跟其后（火山 CED 严格校验）。"""
    cfg = load_config(config_dir=str(tmp_path_factory.mktemp("srv_mrg")))
    os.environ[FAKE_KEY] = "dummy"
    cfg.providers = {
        "Qwen3.8-Max-0902": ProviderSpec(
            logical_name="Qwen3.8-Max-0902", provider="fake",
            base_url=f"http://127.0.0.1:{fake_upstream.port}/v1",
            api_model="qwen-fake", key_env=FAKE_KEY, timeout=10.0),
    }
    cfg.classifier_models = ["Qwen3.8-Max-0902"]
    handler_cls, cfg2, data, classifier = create_app(config=cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        status, _ = _post(url, {
            "model": "Qwen3.8-Max-0902",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "查询天气"}]},
                {"type": "function_call", "call_id": "call_x", "name": "get_weather",
                 "arguments": '{"city": "北京"}'},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "我来查询。"}]},
                {"type": "function_call_output", "call_id": "call_x", "output": "晴 22度"},
            ],
        })
        assert status == 200
        msgs = fake_upstream.received[-1]["messages"]
        # 合并后：assistant 一条（text + tool_calls），tool 紧跟其后
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
        asst = msgs[1]
        assert asst["tool_calls"][0]["function"]["name"] == "get_weather"
        assert asst["content"][0]["text"] == "我来查询。"
        assert msgs[2] == {"role": "tool", "tool_call_id": "call_x", "content": "晴 22度"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_extract_classify_text_content_array():
    """判类文本对 content 数组的 user 消息取文本 parts（不被 tool_result JSON 干扰）。"""
    messages = [
        {"role": "user", "content": [{"type": "input_text", "text": "查询北京的天气"}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "c1",
                      "content": "{\"city\": \"北京\"}"}]},
    ]
    assert _extract_classify_text(messages) == "查询北京的天气"
