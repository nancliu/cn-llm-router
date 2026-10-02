"""OpenAI 兼容 serve 网关（ADR-0021）。

把「判类选型 + 透明转发」暴露为本地 HTTP 端点：Codex / 任意 OpenAI 兼容客户端 /
后续 LiteLLM 桥接（Claude Code）把它作为模型上游消费。

透明转发铁律（ADR-0021）：不改写 messages/tools/参数、不截断流式、tool_call 原样往返；
router 只决定"转发给谁"。零外部依赖（标准库 http.server，与 ADR-0017 Web 面板同栈）。

端点：
- POST /v1/chat/completions   主入口：model="auto"（或缺省）判类路由；model="<logical_name>" 点名透传
- GET  /v1/models             可路由模型（可用 logical_name + auto）
- GET  /health                存活检查

认证：可选 token（CN_LLM_ROUTER_SERVE_TOKEN 或 CLI --token）；设置后要求
Authorization: Bearer <token> 或 X-API-Key: <token>，否则 401。
"""
from __future__ import annotations

import json
import logging
import os
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


class ServeError(Exception):
    """带 HTTP 状态与 OpenAI 风格错误码的网关错误。"""

    def __init__(self, status: int, message: str, code: Optional[str] = None,
                 attempted: Optional[list[str]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or "serve_error"
        self.attempted = attempted or []


def _extract_classify_text(messages) -> str:
    """取最后一条 role=user 且 content 为字符串的消息判类（跳过 tool 结果干扰）。"""
    for m in reversed(messages or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if role == "user" and isinstance(content, str) and content.strip():
            return content
    for m in reversed(messages or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        if role == "user":
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            return str(content)
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
        def _forward(self, rec: Recommendation, kwargs: dict, stream: bool) -> None:
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
                    self._write_completion(resp, stream)
                    return
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
            if path != "/v1/chat/completions":
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
                model_arg = body.get("model", AUTO_MODEL)
                stream = bool(body.get("stream", False))
                text = _extract_classify_text(body.get("messages"))
                rec, clf = self._recommendation(str(model_arg), text)
                kwargs = dict(body)
                kwargs.pop("model", None)
                self._forward(rec, kwargs, stream)
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
               strategy: str = "平衡") -> None:
    """启动 OpenAI 兼容 serve 网关（仅监听 127.0.0.1，不暴露公网）。"""
    handler_cls, cfg, data, classifier = create_app(config, token=token, strategy=strategy)
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
