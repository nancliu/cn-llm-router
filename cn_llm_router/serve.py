"""OpenAI 兼容 serve 网关（ADR-0021）。

把「判类选型 + 透明转发」暴露为本地 HTTP 端点：Codex / 任意 OpenAI 兼容客户端 /
后续 LiteLLM 桥接（Claude Code）把它作为模型上游消费。

透明转发铁律（ADR-0021）：不改写 messages/tools/参数、不截断流式、tool_call 原样往返；
router 只决定"转发给谁"。零外部依赖（标准库 http.server，与 ADR-0017 Web 面板同栈）。

端点：
- POST /v1/chat/completions   主入口：model="auto"（或缺省）判类路由；model="<logical_name>" 点名透传
- POST /v1/responses          Responses API（LiteLLM 桥接 Claude Code 的上游；协议映射到 chat 透明转发）
- GET  /v1/models             可路由模型（可用 logical_name + auto）
- GET  /health                存活检查

认证：可选 token（CN_LLM_ROUTER_SERVE_TOKEN 或 CLI --token）；设置后要求
Authorization: Bearer <token> 或 X-API-Key: <token>，否则 401。
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .classifier import Classifier
from .config import RouterConfig, key_available, load_config
from .data_loader import RouterData, load_data
from .gateway import RouterClient, build_client
from .selector import select as _select
from .types import ModelChoice, Recommendation, RouteError

logger = logging.getLogger("cn_llm_router.serve")

DEFAULT_PORT = 10041
ENV_TOKEN = "CN_LLM_ROUTER_SERVE_TOKEN"
AUTO_MODEL = "auto"


# ---------------------------------------------------------------- 实例检测/重启
def _port_in_use(host: str, port: int) -> bool:
    """探测端口是否已被占用（bind 测试，不产生监听副作用）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _find_pid_on_port(port: int) -> Optional[int]:
    """通过 netstat 找到监听该端口的进程 PID（仅 TCP LISTENING）。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True,
                text=True, timeout=15,
            ).stdout
        except Exception:
            return None
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}"):
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
    return None


def _is_router_process(pid: int) -> bool:
    """判断进程命令行是否为本应用的 serve 实例（cn_llm_router serve）。"""
    cmdline = ""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=15,
            )
            cmdline = (out.stdout or "") + (out.stderr or "")
        except Exception:
            return False
    else:
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=15,
            )
            cmdline = out.stdout or ""
        except Exception:
            return False
    return bool(re.search(r"cn[_-]llm[_-]router.*serve", cmdline, re.IGNORECASE))


def _kill_pid(pid: int) -> bool:
    """终止进程（Windows taskkill /F；POSIX SIGTERM）。"""
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=15,
            )
            return r.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, 15)  # SIGTERM
        return True
    except OSError:
        return False


def _restart_existing_if_needed(host: str, port: int, restart: bool) -> None:
    """启动前检查：若端口已被本应用 serve 实例占用，自动关闭旧实例后重启。

    - 端口空闲 → 正常启动。
    - 端口被**本应用** serve 占用 → 关闭旧实例（restart=False 时跳过并继续，
      由后续 bind 失败自然报错），等待端口释放后由调用方启动新实例。
    - 端口被**其他程序**占用 → 直接 SystemExit 报错，不自动处理。
    """
    if not _port_in_use(host, port):
        return
    pid = _find_pid_on_port(port)
    if pid is None:
        raise SystemExit(
            f"端口 {host}:{port} 已被占用，但未能识别占用进程，请先释放端口或改用 --port"
        )
    if _is_router_process(pid):
        if not restart:
            print(f"检测到已有 cn-llm-router serve 实例（PID {pid}）监听 {host}:{port}，"
                  f"--no-restart 已指定，跳过自动重启")
            return
        print(f"检测到已有 cn-llm-router serve 实例（PID {pid}）监听 {host}:{port}，"
              f"自动关闭旧实例后重启…")
        if not _kill_pid(pid):
            raise SystemExit(f"关闭旧 serve 实例（PID {pid}）失败，请手动停止后重试")
        # 等待端口释放（Windows taskkill 异步，最多 5s）
        for _ in range(50):
            if not _port_in_use(host, port):
                break
            time.sleep(0.1)
        return
    raise SystemExit(
        f"端口 {host}:{port} 已被其他进程（PID {pid}）占用，为避免误杀未自动处理；"
        f"请释放端口或改用 --port"
    )


class ServeError(Exception):
    """带 HTTP 状态与 OpenAI 风格错误码的网关错误。"""

    def __init__(self, status: int, message: str, code: Optional[str] = None,
                 attempted: Optional[list[str]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or "serve_error"
        self.attempted = attempted or []


def _content_to_text(content) -> str:
    """把 chat/Responses 的 content（str / list parts / dict）归一为判类文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("type")
                if t in ("text", "input_text", "output_text", "tool_result"):
                    parts.append(str(p.get("text", "")))
                elif p.get("text"):
                    parts.append(str(p["text"]))
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(parts)
    if isinstance(content, dict) and content.get("text"):
        return str(content["text"])
    return ""


def _extract_classify_text(messages) -> str:
    """取最后一条 role=user 且含文本的消息判类（跳过 tool 结果干扰）。"""
    for m in reversed(messages or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if role == "user":
            text = _content_to_text(content)
            if text.strip():
                return text
    return ""


def _named_choice(data: RouterData, logical: str, rank: int, reason: str) -> ModelChoice:
    """按 logical_name 构造 ModelChoice（点名模式；score=0 表示不参与评分）。"""
    spec = data.models.get(logical)
    if spec is not None:
        return ModelChoice(
            logical_name=logical, vendor=spec.vendor, score=0.0,
            cost=spec.cost if spec.cost is not None else 0.0,
            rank=rank, reason=reason,
        )
    return ModelChoice(logical_name=logical, vendor="", score=0.0, cost=0.0, rank=rank, reason=reason)


def _make_handler(cfg: RouterConfig, data: RouterData, classifier: Classifier,
                  token: Optional[str], strategy: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cn-llm-router-serve/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # access log 走 logging（不刷 stdout）
            logger.debug("%s - %s", self.address_string(), fmt % args)

        # ---------- 认证 ----------
        def _authorized(self) -> bool:
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            key = self.headers.get("X-API-Key", "")
            return auth == f"Bearer {token}" or key == token

        # ---------- 响应 ----------
        def _send_json(self, status: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_openai_error(self, e: ServeError) -> None:
            self._send_json(e.status, {
                "error": {"message": e.message, "type": "router_error",
                          "code": e.code, "attempted": e.attempted},
            })

        # ---------- 路由 ----------
        def _recommendation(self, model_arg: str, text: str) -> tuple[Recommendation, object]:
            """判类 + 选型：auto → select 推荐；logical_name → 点名主选 + 同类别备选。"""
            clf = classifier.classify(text)
            if model_arg in (None, "", AUTO_MODEL):
                try:
                    rec = _select(data, clf.category, clf.complexity, strategy=strategy,
                                  cfg=cfg, availability_filter=True)
                except ValueError as e:
                    raise ServeError(400, str(e), code="no_available_models") from None
                return rec, clf

            prov = cfg.providers.get(model_arg)
            if prov is None:
                raise ServeError(400, f"未知模型 {model_arg}（config/providers.yaml 中不存在）",
                                 code="unknown_model")
            if not key_available(prov):
                raise ServeError(400, f"模型 {model_arg} 的 API key 未配置（环境变量 {prov.key_env}）",
                                 code="missing_key")
            # 判类仍执行：用于取类别上下文与同类别备选（failover 不换到类别外）
            try:
                rec = _select(data, clf.category, clf.complexity, strategy=strategy,
                              cfg=cfg, availability_filter=True)
            except ValueError:
                rec = None
            backup = None
            if rec is not None and rec.backup is not None and rec.backup.logical_name != model_arg:
                backup = rec.backup
            named = _named_choice(data, model_arg, rank=0, reason="用户点名（不参与评分矩阵）")
            return Recommendation(strategy=strategy, availability_filtered=True,
                                   primary=named, backup=backup), clf

        # ---------- 转发（透明：tools/stream/参数原样，model 由上游 api_model 覆盖） ----------
        def _forward_raw(self, rec: Recommendation, kwargs: dict, stream: bool):
            """按主选→备选链转发，返回上游响应对象（失败抛 ServeError 502）。"""
            chain = [rec.primary] + ([rec.backup] if rec.backup else [])
            attempted: list[str] = []
            last_err: Optional[Exception] = None
            for choice in chain:
                prov = cfg.providers.get(choice.logical_name)
                if prov is None:
                    last_err = ServeError(502, f"模型 {choice.logical_name} 未配置 provider",
                                          code="no_provider", attempted=attempted)
                    continue
                if not key_available(prov):
                    continue  # 备选缺 key 跳过（主选缺 key 已在 _recommendation 拦截）
                client = build_client(prov)
                kw = dict(kwargs)
                kw["model"] = prov.api_model
                attempted.append(choice.logical_name)
                try:
                    resp = client.chat.completions.create(**kw)
                    return resp
                except Exception as e:  # noqa: BLE001 —— 按类型判定是否可切
                    last_err = e
                    if not RouterClient._is_retryable(e) or choice is chain[-1]:
                        break
                    logger.info("serve 模型 %s 调用失败，切备选: %s", choice.logical_name, e)
                    continue
            raise ServeError(502, f"上游模型调用最终失败: {type(last_err).__name__}: {last_err}",
                             code="upstream_failed", attempted=attempted)

        def _write_completion(self, resp, stream: bool) -> None:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for chunk in resp:
                        data = json.dumps(chunk.model_dump(), ensure_ascii=False).encode("utf-8")
                        self.wfile.write(b"data: " + data + b"\n\n")
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                finally:
                    self.close_connection = True
            else:
                self._send_json(200, resp.model_dump())

        # ---------- Responses API（/v1/responses，LiteLLM 桥接上游） ----------
        # 转换边界：litellm 已把 Anthropic /v1/messages 转成 Responses 请求发到这里；
        # 本层把 Responses 请求映射回 OpenAI chat 格式做透明转发（上游国产模型只有 chat API），
        # 再把上游 chat 响应组装成 Responses 响应。内容/工具调用原样透传，只做协议映射。
        def _responses_input_to_messages(self, items) -> list[dict]:
            messages: list[dict] = []
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                t = it.get("type")
                if t == "message":
                    role = it.get("role", "user")
                    content = self._responses_content_to_chat(it.get("content"))
                    if content is not None or role == "assistant":
                        messages.append({"role": role, "content": content})
                elif t == "function_call":
                    call_id = it.get("call_id") or it.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    fn = {"name": it.get("name") or "",
                          "arguments": str(it.get("arguments") or "{}")}
                    messages.append({"role": "assistant", "content": None,
                                     "tool_calls": [{"id": call_id, "type": "function",
                                                     "function": fn}]})
                elif t == "function_call_output":
                    messages.append({"role": "tool", "tool_call_id": it.get("call_id"),
                                     "content": str(it.get("output") or "")})
                # reasoning / 其他类型跳过
            return self._merge_consecutive(messages)

        @staticmethod
        def _merge_consecutive(messages: list[dict]) -> list[dict]:
            """合并相邻同角色消息：litellm 会把 tool_use 拆成 function_call 独立 item，
            与 assistant 文本 item 交错产生『tool_calls assistant 后插入第二条 assistant』，
            严格上游（火山 CED）会 400 拒绝。合并后 tool_calls 紧跟对应 tool 消息。"""
            merged: list[dict] = []
            for m in messages:
                if (merged and merged[-1].get("role") == m.get("role")
                        and m.get("role") in ("assistant", "user", "system")):
                    prev = merged[-1]
                    # content 合并
                    if m.get("content") is not None and prev.get("content") is None:
                        prev["content"] = m["content"]
                    elif m.get("content") is not None and prev.get("content") is not None:
                        a = prev["content"]
                        b = m["content"]
                        if isinstance(a, str) and isinstance(b, str):
                            prev["content"] = a + "\n" + b
                        else:
                            prev["content"] = [a] if not isinstance(a, list) else list(a)
                            b_list = [b] if not isinstance(b, list) else list(b)
                            prev["content"].extend(b_list)
                    # tool_calls 合并
                    prev.setdefault("tool_calls", None)
                    if m.get("tool_calls"):
                        prev["tool_calls"] = list(prev.get("tool_calls") or []) + list(m["tool_calls"])
                else:
                    merged.append(dict(m))
            return merged

        def _responses_content_to_chat(self, content):
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type")
                    if pt in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": p.get("text", "")})
                    elif pt == "input_image":
                        url = p.get("image_url") or p.get("file_id") or p.get("data") or ""
                        parts.append({"type": "image_url",
                                      "image_url": {"url": str(url)}})
                return parts if parts else None
            return None

        def _responses_tools_to_chat(self, tools):
            out = []
            for t in tools or []:
                if not isinstance(t, dict) or t.get("type") != "function":
                    continue
                fn = {"name": t.get("name", ""), "parameters": t.get("parameters", {})}
                if t.get("description"):
                    fn["description"] = t["description"]
                out.append({"type": "function", "function": fn})
            return out or None

        def _chat_to_responses(self, chat: dict, req_model: str) -> dict:
            choices = chat.get("choices") or []
            msg = (choices[0].get("message") if choices else {}) or {}
            text = msg.get("content") or ""
            output: list[dict] = []
            if text:
                output.append({
                    "type": "message", "id": f"msg_{uuid.uuid4().hex[:8]}",
                    "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                })
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                output.append({
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:8]}",
                    "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": fn.get("name", ""),
                    "arguments": str(fn.get("arguments") or "{}"),
                    "status": "completed", "arguments_status": "completed",
                })
            usage = chat.get("usage") or {}
            return {
                "id": f"resp_{uuid.uuid4().hex[:12]}", "object": "response",
                "created_at": int(time.time()), "status": "completed",
                "model": chat.get("model") or req_model,
                "output": output,
                "output_text": text,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }

        def _send_responses_sse(self, resp: dict) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def ev(t: str, **kw) -> bytes:
                return ("data: " + json.dumps({"type": t, **kw},
                                              ensure_ascii=False) + "\n\n").encode("utf-8")

            base = {k: v for k, v in resp.items() if k != "output"}
            base["output"] = []
            try:
                self.wfile.write(ev("response.created", response=base))
                self.wfile.flush()
                self.wfile.write(ev("response.in_progress", response=base))
                self.wfile.flush()
                for i, item in enumerate(resp["output"]):
                    self.wfile.write(ev("response.output_item.added",
                                        output_index=i, item=item))
                    self.wfile.flush()
                    if item["type"] == "message":
                        part = item["content"][0]
                        self.wfile.write(ev("response.content_part.added", item_id=item["id"],
                                            output_index=i, content_index=0, part=part))
                        self.wfile.flush()
                        text = part.get("text", "")
                        self.wfile.write(ev("response.output_text.delta", item_id=item["id"],
                                            output_index=i, content_index=0, delta=text))
                        self.wfile.flush()
                        self.wfile.write(ev("response.output_text.done", item_id=item["id"],
                                            output_index=i, content_index=0, text=text))
                        self.wfile.flush()
                        self.wfile.write(ev("response.content_part.done", item_id=item["id"],
                                            output_index=i, content_index=0, part=part))
                        self.wfile.flush()
                    self.wfile.write(ev("response.output_item.done", output_index=i, item=item))
                    self.wfile.flush()
                self.wfile.write(ev("response.completed", response=resp))
                self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            finally:
                self.close_connection = True

        def _handle_responses(self, body: dict) -> None:
            model_arg = body.get("model", AUTO_MODEL)
            stream = bool(body.get("stream", False))
            inp = body.get("input", "")
            if isinstance(inp, str):
                messages = [{"role": "user", "content": inp}]
            elif isinstance(inp, list):
                messages = self._responses_input_to_messages(inp)
            else:
                messages = []
            instructions = body.get("instructions")
            if instructions:
                system = next((m for m in messages if m.get("role") == "system"), None)
                if system is None:
                    messages.insert(0, {"role": "system", "content": instructions})
                else:
                    system["content"] = str(system.get("content") or "") + "\n" + instructions
            text = _extract_classify_text(messages)
            rec, _clf = self._recommendation(str(model_arg), text)
            kwargs: dict = {"messages": messages, "stream": False}  # 内部聚合，输出按需转 SSE
            tools = self._responses_tools_to_chat(body.get("tools"))
            if tools:
                kwargs["tools"] = tools
            for k in ("temperature", "top_p"):
                if k in body:
                    kwargs[k] = body[k]
            if body.get("max_output_tokens"):
                kwargs["max_tokens"] = body["max_output_tokens"]
            chat = self._forward_raw(rec, kwargs, stream=False)
            resp = self._chat_to_responses(chat.model_dump(), str(model_arg))
            if stream:
                self._send_responses_sse(resp)
            else:
                self._send_json(200, resp)

        # ---------- HTTP ----------
        def do_GET(self):
            if not self._authorized():
                self._send_openai_error(ServeError(401, "未授权：请提供 Bearer token 或 X-API-Key",
                                                   code="unauthorized"))
                return
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send_json(200, {"status": "ok", "strategy": strategy})
                return
            if path == "/v1/models":
                models = [AUTO_MODEL] + sorted(
                    logical for logical, prov in cfg.providers.items() if key_available(prov)
                )
                self._send_json(200, {"object": "list", "data": [
                    {"id": m, "object": "model", "owned_by": "cn-llm-router"} for m in models
                ]})
                return
            self._send_openai_error(ServeError(404, f"未知路径 {path}", code="not_found"))

        def do_POST(self):
            if not self._authorized():
                self._send_openai_error(ServeError(401, "未授权：请提供 Bearer token 或 X-API-Key",
                                                   code="unauthorized"))
                return
            path = self.path.split("?", 1)[0]
            if path not in ("/v1/chat/completions", "/v1/responses"):
                self._send_openai_error(ServeError(404, f"未知路径 {path}", code="not_found"))
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                self._send_openai_error(ServeError(400, f"请求体不是合法 JSON: {e}",
                                                   code="invalid_request"))
                return
            if not isinstance(body, dict):
                self._send_openai_error(ServeError(400, "请求体应为 JSON 对象", code="invalid_request"))
                return
            try:
                if path == "/v1/responses":
                    self._handle_responses(body)
                    return
                model_arg = body.get("model", AUTO_MODEL)
                stream = bool(body.get("stream", False))
                text = _extract_classify_text(body.get("messages"))
                rec, clf = self._recommendation(str(model_arg), text)
                kwargs = dict(body)
                kwargs.pop("model", None)
                resp = self._forward_raw(rec, kwargs, stream)
                self._write_completion(resp, stream)
            except ServeError as e:
                self._send_openai_error(e)
            except Exception as e:  # noqa: BLE001 —— 兜底不裸奔
                logger.exception("serve 处理异常")
                self._send_openai_error(ServeError(500, f"内部错误: {type(e).__name__}",
                                                   code="internal_error"))

    return Handler


def create_app(config: RouterConfig | None = None, *, token: Optional[str] = None,
               strategy: str = "平衡"):
    """构造处理器类与共享的 cfg/data/classifier（测试可直接复用）。

    token 缺省读环境变量 CN_LLM_ROUTER_SERVE_TOKEN（显式传 None 之外的字符串即启用）。
    """
    cfg = config or load_config()
    data = load_data(cfg.data_dir, weights_override=cfg.weights_override)
    classifier = Classifier(data=data, cfg=cfg)
    tok = token if token is not None else os.environ.get(ENV_TOKEN, "")
    handler_cls = _make_handler(cfg, data, classifier, tok or None, strategy)
    return handler_cls, cfg, data, classifier


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
               config: RouterConfig | None = None, *, token: Optional[str] = None,
               strategy: str = "平衡", restart_existing: bool = True) -> None:
    """启动 OpenAI 兼容 serve 网关（仅监听 127.0.0.1，不暴露公网）。

    restart_existing=True（默认）：若目标端口已被**本应用的 serve 实例**占用，
    自动关闭旧实例再启动新实例（避免多实例抢端口导致路由混乱）。
    端口被其他程序占用时直接报错退出，不自动处理。
    """
    handler_cls, cfg, data, classifier = create_app(config, token=token, strategy=strategy)
    _restart_existing_if_needed(host, port, restart_existing)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True
    print(f"cn-llm-router serve 网关已启动：http://{host}:{port}/v1  （Ctrl+C 退出）")
    print(f"路由模式: model=auto 判类路由 / model=<logical_name> 点名透传；策略={strategy}")
    if token:
        print("认证: 已启用（Authorization: Bearer <token> 或 X-API-Key）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
