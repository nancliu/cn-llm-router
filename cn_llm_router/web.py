"""本地面板（ADR-0017）：标准库 http.server 实现，零外部依赖，仅监听 127.0.0.1。

四个页面：
- GET  /        首页（推荐查询表单 + 12 类定义 + 模型注册表[含本机状态]）
- GET  /routes  网关路由（serve 10041 / litellm 4000 在线状态 + 最近实际路由决策 + 分类评测）
- POST /route   推荐查询：持久 Classifier 分类 → selector.select 选模型
- GET  /cost    成本报表（复用 scripts/cost_report.py 的汇总逻辑）
- GET  /cache   缓存状态（配置 + 本进程实例 hits/misses/size）

纯 HTML + 内联 CSS，不引入前端框架；不修改 classify/select/route 签名与行为。
"""
from __future__ import annotations

import html as _html
import json as _json
import os
import sys
import urllib.request as _urllib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .classifier import Classifier
from .config import RouterConfig, key_available, load_config
from .data_loader import RouterData, load_data
from .selector import select as _select
from .types import STRATEGIES


def _import_cost_report():
    """复用 scripts/cost_report.py 的 load_events/filter_month/summarize（ADR-0012 同款跨目录导入）。"""
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import cost_report  # noqa: E402 —— scripts/ 不在包路径
    return cost_report


_CSS = """
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       margin: 0; background: #f5f6f8; color: #222; line-height: 1.6; }
nav { background: #1f2937; padding: 12px 24px; }
nav a { color: #e5e7eb; margin-right: 18px; text-decoration: none; font-size: 15px; }
nav a:hover { color: #fff; }
main { max-width: 960px; margin: 24px auto; padding: 0 20px 40px; }
.card { background: #fff; border-radius: 8px; padding: 20px 24px; margin-bottom: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }
h1 { font-size: 22px; margin-top: 0; }
h2 { font-size: 17px; border-left: 3px solid #2563eb; padding-left: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; margin: 8px 0; }
th, td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
th { background: #f3f4f6; }
textarea, select, input[type=text] { padding: 8px; border: 1px solid #d1d5db;
        border-radius: 6px; font-size: 14px; }
textarea { width: 100%; min-height: 70px; box-sizing: border-box; }
button { background: #2563eb; color: #fff; border: 0; padding: 9px 20px;
        border-radius: 6px; font-size: 14px; cursor: pointer; }
button:hover { background: #1d4ed8; }
.muted { color: #6b7280; font-size: 13px; }
.tag { display: inline-block; background: #eff6ff; color: #1d4ed8; border-radius: 4px;
        padding: 2px 8px; font-size: 13px; margin-right: 6px; }
.warn { background: #fffbeb; border: 1px solid #fcd34d; color: #92400e;
        padding: 10px 14px; border-radius: 6px; }
.ok { color: #166534; font-weight: 600; }
.off { color: #b91c1c; font-weight: 600; }
.pend { color: #6b7280; }
code { background: #f3f4f6; padding: 1px 6px; border-radius: 4px; font-size: 13px; }
footer { color: #6b7280; font-size: 12.5px; padding: 16px 20px; text-align: center; }
"""


def _shell(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head><body>"
        "<nav><a href=\"/\">推荐查询</a><a href=\"/routes\">网关路由</a>"
        "<a href=\"/cost\">成本报表</a><a href=\"/cache\">缓存状态</a></nav><main>"
        f"{body}"
        "</main><footer>国内大模型选择器 cn-llm-router · 打分表 v1-20260923（12 类 × 3 级复杂度 × 三档性价比开关）"
        "· 评分/价格数字可溯源，缺口标「待补充」· <a href=\"https://github.com/nancliu/cn-llm-router\">GitHub</a> · "
        "<a href=\"https://feishu.doubao.com/sheets/Ku08sUbNHhtNggtaa82c99qYnwe\">打分表</a></footer>"
        "</body></html>"
    )


def _group_table(rows: list[dict]) -> str:
    """渲染成本报表分组表（by_model / by_category / by_event_type 通用）。"""
    if not rows:
        return "<p class=\"muted\">（无）</p>"
    out = [
        "<table><tr><th>名称</th><th>次数</th><th>成本(元)</th>"
        "<th>次数占比</th><th>成本占比</th></tr>"
    ]
    for r in rows:
        out.append(
            "<tr>"
            f"<td>{_html.escape(str(r['name']))}</td>"
            f"<td>{r['count']}</td>"
            f"<td>{r['cost_yuan']:.4f}</td>"
            f"<td>{r['count_share'] * 100:.1f}%</td>"
            f"<td>{r['cost_share'] * 100:.1f}%</td>"
            "</tr>"
        )
    out.append("</table>")
    return "".join(out)


def _make_handler(cfg: RouterConfig, data: RouterData, classifier: Classifier):
    """构造绑定当前 cfg/data/classifier 的请求处理器类。"""
    cr = _import_cost_report()

    def _choice_row(label: str, m) -> str:
        cost = f"{m.cost:.2f} 元/百万tok" if m.cost is not None else "缺价"
        return (
            f"<p><b>{label}</b>：{_html.escape(m.logical_name)}（{_html.escape(m.vendor)}）"
            f" — 能力分 {m.score:.1f}，成本 {cost}<br>"
            f"<span class=\"muted\">理由：{_html.escape(m.reason)}</span></p>"
        )

    def _foreign_ref_section() -> str:
        """ADR-0019：国外主流模型参照（仅展示，不参与路由；真实实测留社区）。"""
        foreign = [f for f in data.foreign_models if f.region == "国外"]
        if not foreign:
            return ""
        rows = []
        for f in foreign:
            elo = f"{f.lmarena_elo:.0f}" if f.lmarena_elo is not None else "待补充"
            cost = f"{f.cost:.2f} 元/百万tok" if f.cost is not None else "缺价"
            rows.append(
                "<tr>"
                f"<td>{_html.escape(f.model)}</td>"
                f"<td style='text-align:right'>{elo}</td>"
                f"<td style='text-align:right'>{cost}</td>"
                f"<td>{_html.escape(f.availability)}</td>"
                f"<td>{_html.escape(f.open_source)}</td>"
                "</tr>"
            )
        return (
            "<div class=\"card\"><h2>国外主流模型参照</h2>"
            "<table><tr><th>模型</th><th style='text-align:right'>LMArena Elo</th>"
            "<th style='text-align:right'>综合成本</th><th>国内可用性</th><th>开源</th></tr>"
            f"{''.join(rows)}</table>"
            "<p class=\"muted\">国产头部 Elo 1481~1500 vs 国外最高约 1525（同量级，差距 1.7%~3%）；"
            "同档旗舰成本国产便宜 8.7~42 倍；国产全部官方直连、无合规风险。"
            "参照仅展示、不参与路由（口径与来源见打分表「国内外对照」Sheet）。</p>"
            "</div>"
        )

    def _render_result(clf, rec) -> str:
        conf_tag = "（低置信）" if clf.low_confidence else ""
        cached_tag = "（缓存命中）" if clf.cached else ""
        fb = f"，{clf.fallback_reason}" if clf.fallback_reason else ""
        parts = [
            "<p>类别：<span class=\"tag\">" + _html.escape(clf.category) + "</span>"
            "复杂度：<span class=\"tag\">" + _html.escape(clf.complexity) + "</span>"
            f"置信度：{clf.confidence:.2f}{conf_tag}{cached_tag}{fb}</p>"
        ]
        if clf.second_guess:
            parts.append(f"<p class=\"muted\">次选类别：{_html.escape(clf.second_guess)}</p>")
        parts.append(_choice_row("主选", rec.primary))
        if rec.backup is not None:
            parts.append(_choice_row("备选", rec.backup))
        for n in rec.notice:
            parts.append(f"<p class=\"warn\">提示：{_html.escape(n)}</p>")
        parts.append(_foreign_ref_section())
        return "".join(parts)

    def _registry_section() -> str:
        cat_rows = "".join(
            f"<tr><td>{_html.escape(c.name)}</td><td>{_html.escape(c.description)}</td></tr>"
            for c in data.categories.values()
        )
        mrows = []
        for m in data.models.values():
            pin = f"{m.price_in:.2f}" if m.price_in is not None else "—"
            pout = f"{m.price_out:.2f}" if m.price_out is not None else "—"
            prov = cfg.providers.get(m.logical_name)
            if prov is None:
                status = "<span class=\"pend\">未配置 key</span>"
            elif not prov.enabled:
                status = "<span class=\"off\">已停用（enabled=false）</span>"
            elif key_available(prov):
                status = "<span class=\"ok\">可用</span>"
            else:
                status = "<span class=\"pend\">缺 key</span>"
            mrows.append(
                "<tr>"
                f"<td>{_html.escape(m.logical_name)}</td>"
                f"<td>{_html.escape(m.vendor)}</td>"
                f"<td style='text-align:right'>{pin}</td>"
                f"<td style='text-align:right'>{pout}</td>"
                f"<td>{status}</td>"
                "</tr>"
            )
        return (
            "<div class=\"card\"><h2>12 类任务定义</h2>"
            f"<table><tr><th>类别</th><th>说明</th></tr>{cat_rows}</table></div>"
            "<div class=\"card\"><h2>模型注册表</h2>"
            "<table><tr><th>逻辑模型名</th><th>厂商</th><th style='text-align:right'>输入价</th>"
            f"<th style='text-align:right'>输出价</th><th>本机状态</th></tr>{''.join(mrows)}</table>"
            "<p class=\"muted\">价格单位：元/百万 tokens（ADR-0001 口径）。"
            "「已停用」= config/providers.yaml 中 enabled=false（如套餐未续费）；"
            "「缺 key」= 已配置 provider 但环境变量无对应 API key，不参与路由。</p></div>"
        )

    def _fetch_json(url: str, timeout: float = 3.0) -> dict | None:
        try:
            with _urllib.urlopen(url, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 —— 探测失败一律视为离线
            return None

    def _gateway_status_section() -> str:
        """serve 10041 + litellm 4000 在线状态与可用模型（本地面板自检网关链路）。"""
        serve_alive = _fetch_json("http://127.0.0.1:10041/health") is not None
        litellm_ok = False
        litellm_models: list[str] = []
        try:
            req = _urllib.Request(
                "http://127.0.0.1:4000/v1/models",
                headers={"Authorization": "Bearer sk-router-bridge"},
            )
            with _urllib.urlopen(req, timeout=3) as r:
                litellm_ok = True
                j = _json.loads(r.read().decode("utf-8"))
                litellm_models = [str(m.get("id", "")) for m in j.get("data", []) if m.get("id")]
        except Exception:  # noqa: BLE001 —— 离线
            pass
        serve_tag = "<span class='ok'>在线</span>" if serve_alive else "<span class='off'>离线</span>"
        lit_tag = "<span class='ok'>在线</span>" if litellm_ok else "<span class='off'>离线</span>"
        start_hint = (
            "<p class=\"muted\">serve 未启动时：<code>powershell -ExecutionPolicy Bypass "
            "-File scripts/start-gateway.ps1</code>（清 DATABASE_URL → 起 serve → 起 litellm → 就绪探测）。"
            "已注册自愈计划任务：每 10 分钟检查 + 登录自启。</p>"
        )
        if litellm_models:
            models_html = (
                "<p>litellm 可用模型："
                + "".join(f"<span class=\"tag\">{_html.escape(m)}</span>" for m in litellm_models)
                + "</p>"
            )
        else:
            models_html = ""
        return (
            "<div class=\"card\"><h2>网关状态</h2>"
            "<table><tr><th>组件</th><th>地址</th><th>状态</th></tr>"
            f"<tr><td>serve（判类路由网关）</td><td>127.0.0.1:10041</td><td>{serve_tag}</td></tr>"
            f"<tr><td>litellm（Claude 格式桥接）</td><td>127.0.0.1:4000</td><td>{lit_tag}</td></tr>"
            f"</table>{models_html}{start_hint if not serve_alive else ''}</div>"
        )

    def _routes_page() -> str:
        """最近路由决策（GET /v1/routes，最新在前）：实际选了什么模型、耗时、tokens。"""
        status = _gateway_status_section()
        j = _fetch_json("http://127.0.0.1:10041/v1/routes")
        if j is None:
            routes_html = (
                "<div class=\"card\"><h2>最近路由决策</h2>"
                "<p class=\"warn\">serve 网关未启动，无法读取路由历史。"
                "启动后 Cliude Code / Codex 每请求都会在 serve 内存记录最近 50 条（GET /v1/routes）。</p></div>"
            )
        else:
            rows = j.get("data", [])
            if not rows:
                routes_html = (
                    "<div class=\"card\"><h2>最近路由决策</h2>"
                    "<p class=\"muted\">暂无路由记录——serve 启动后尚未收到任何请求（或重启后清空）。"
                    "用 Claude Code / Codex 发一次请求后刷新本页即可看到。</p></div>"
                )
            else:
                def _esc(x):
                    return _html.escape(str(x)) if x is not None else "—"
                trs = []
                for e in rows:
                    failed = e.get("status") in ("failed", "error")
                    model_cell = (
                        f"<span class='off'>{_esc(e.get('model'))}</span>"
                        f"<br><span class='muted'>{_esc(e.get('error'))}</span>"
                        if failed else
                        f"{_esc(e.get('model'))}<br><span class='muted'>api={_esc(e.get('api_model'))}</span>"
                    )
                    trs.append(
                        "<tr>"
                        f"<td>{_esc(e.get('ts'))}</td>"
                        f"<td>{_esc(e.get('category'))} · {_esc(e.get('complexity'))}</td>"
                        f"<td>{model_cell}</td>"
                        f"<td>{_esc(e.get('strategy'))} / {_esc(e.get('mode'))}</td>"
                        f"<td style='text-align:right'>{_esc(e.get('elapsed_s'))}</td>"
                        f"<td style='text-align:right'>{_esc(e.get('tokens'))}</td>"
                        f"<td>{'<span class=\'off\'>失败</span>' if failed else '<span class=\'ok\'>成功</span>'}</td>"
                        "</tr>"
                    )
                routes_html = (
                    "<div class=\"card\"><h2>最近路由决策（最新在前，最多 50 条）</h2>"
                    "<table><tr><th>时间</th><th>类别 · 复杂度</th><th>选中模型</th>"
                    "<th>策略 / 模式</th><th style='text-align:right'>耗时 s</th>"
                    "<th style='text-align:right'>tokens</th><th>结果</th></tr>"
                    f"{''.join(trs)}</table>"
                    "<p class=\"muted\">模式：auto=判类路由 / named=点名透传+同类别备选兜底。"
                    "失败行显示上游尝试过的模型（attempted）与错误摘要。</p></div>"
                )
        body = (
            "<div class=\"card\"><h1>网关路由 · 实际选了什么模型</h1>"
            "<p class=\"muted\">本页查看 serve 网关最近实际路由决策（与 Claude Code / Codex 请求一一对应）。"
            "推荐查询页给出「应该用哪个」；本页给出「实际用了哪个」。</p></div>"
            f"{status}{routes_html}"
            "<div class=\"card\"><h2>分类评测</h2>"
            "<p>180 条 golden cases 全量评测：Qwen 分类器修复后 <b>100%</b> 通过"
            "（reports/eval-qwen-fix-20260930.json；ADR-0020 规则修复）。"
            "无 key 时分类走确定性规则关键词兜底，面板仍可用。</p></div>"
        )
        return _shell("网关路由 · cn-llm-router", body)

    def _home_page(prompt: str, strategy: str, availability_filter: bool,
                   result_html: str) -> str:
        opts = "".join(
            f'<option value="{_html.escape(s)}"{" selected" if s == strategy else ""}>'
            f"{_html.escape(s)}</option>"
            for s in STRATEGIES
        )
        form = (
            "<div class=\"card\"><h1>推荐查询</h1>"
            "<form method=\"post\" action=\"/route\" "
            "onsubmit=\"this.querySelector('button').disabled=true;"
            "document.getElementById('busy').style.display='block';return true;\">"
            "<p><textarea name=\"prompt\" placeholder=\"输入你的任务 prompt，例如：写一个 Python 函数解析 JSON\">"
            f"{_html.escape(prompt)}</textarea></p>"
            "<p>策略：<select name=\"strategy\">"
            f"{opts}</select>"
            "&nbsp;&nbsp;<label><input type=\"checkbox\" name=\"availability_filter\""
            f"{' checked' if availability_filter else ''}> "
            "可用性过滤（仅推荐已配 key 的模型）</label></p>"
            "<p><button type=\"submit\">查询推荐</button></p>"
            "</form>"
            "<p id=\"busy\" class=\"warn\" style=\"display:none\">⏳ 处理中……"
            "分类调用约需 10~30 秒（默认 Qwen 分类模型），请勿重复提交。</p>"
            "</div>"
        )
        body = form
        if result_html:
            body += f"<div class=\"card\"><h2>推荐结果</h2>{result_html}</div>"
        body += _registry_section()
        return _shell("推荐查询 · cn-llm-router", body)

    class Handler(BaseHTTPRequestHandler):
        server_version = "cn-llm-router-web/0.1"

        def log_message(self, fmt, *args):  # noqa: A003 —— 静默本地访问日志
            pass

        # ----- 响应工具 -----
        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, code: int, text: str) -> None:
            self._send(code, "text/html; charset=utf-8", text.encode("utf-8"))

        # ----- 路由 -----
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._html(200, _home_page("", "平衡", cfg.availability_filter, ""))
                elif parsed.path == "/routes":
                    self._html(200, _routes_page())
                elif parsed.path == "/cost":
                    self._render_cost(parsed.query)
                elif parsed.path == "/cache":
                    self._render_cache()
                else:
                    self._html(404, _shell("404", "<div class=\"card\"><h1>404 Not Found</h1></div>"))
            except Exception as e:  # noqa: BLE001 —— 面板不崩，回 500 页面
                self._html(
                    500,
                    _shell("错误", f"<div class=\"card\"><h1>内部错误</h1>"
                              f"<p class=\"warn\">{_html.escape(str(e))}</p></div>"),
                )

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/route":
                self._html(404, _shell("404", "<div class=\"card\"><h1>404 Not Found</h1></div>"))
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = parse_qs(raw)
            prompt = form.get("prompt", [""])[0].strip()
            strategy = form.get("strategy", ["平衡"])[0]
            availability_filter = form.get("availability_filter", [""])[0] == "on"
            self._handle_route(prompt, strategy, availability_filter)

        # ----- POST /route -----
        def _handle_route(self, prompt: str, strategy: str, availability_filter: bool) -> None:
            if not prompt:
                result_html = "<div class=\"warn\">请输入 prompt。</div>"
            else:
                try:
                    clf = classifier.classify(prompt)  # 复用长驻实例，缓存可累积命中
                    rec = _select(
                        data, clf.category, clf.complexity,
                        strategy=strategy, cfg=cfg,
                        availability_filter=availability_filter,
                    )
                    result_html = _render_result(clf, rec)
                except ValueError as e:
                    result_html = (
                        f"<div class=\"warn\">{_html.escape(str(e))}<br>"
                        "提示：未配置任何 API key 时，可关闭「可用性过滤」查看全量推荐。</div>"
                    )
            self._html(200, _home_page(prompt, strategy, availability_filter, result_html))

        # ----- GET /cost -----
        def _render_cost(self, query: str) -> None:
            qs = parse_qs(query)
            month = qs.get("month", [None])[0]
            events = cr.load_events(str(cfg.stats_log_path))
            if not events:
                body = (
                    "<div class=\"card\"><h1>成本报表</h1>"
                    "<p>暂无统计数据（日志文件不存在或为空）。</p>"
                    "<p class=\"muted\">启用 stats_enabled 后开始记录："
                    "config/selector.yaml 设 stats_enabled=true，或 CLI 调用加 --stats。"
                    f"当前日志路径：{_html.escape(str(cfg.stats_log_path))}</p></div>"
                )
                self._html(200, _shell("成本报表", body))
                return
            if month is None:
                month = datetime.now().strftime("%Y-%m")
            selected = cr.filter_month(events, month)
            s = cr.summarize(selected)
            unknown = (
                f"　缺价条目：{s['unknown_cost_events']}" if s["unknown_cost_events"] else ""
            )
            body = (
                "<div class=\"card\"><h1>成本报表 · " + _html.escape(month) + "</h1>"
                "<form method=\"get\" action=\"/cost\">"
                "<label>月份 <input type=\"text\" name=\"month\" value=\""
                f"{_html.escape(month)}\" placeholder=\"YYYY-MM\" size=\"10\"></label> "
                "<button type=\"submit\">查询</button></form>"
                f"<p>事件总数：<b>{s['total_events']}</b>　"
                f"总成本：<b>{s['total_cost_yuan']:.4f} 元</b>{unknown}</p></div>"
                "<div class=\"card\"><h2>按模型</h2>" + _group_table(s["by_model"]) + "</div>"
                "<div class=\"card\"><h2>按类别</h2>" + _group_table(s["by_category"]) + "</div>"
                "<div class=\"card\"><h2>按事件类型</h2>" + _group_table(s["by_event_type"]) + "</div>"
            )
            self._html(200, _shell(f"成本报表 · {month}", body))

        # ----- GET /cache -----
        def _render_cache(self) -> None:
            c = classifier.cache
            if c is None:
                stats_html = "<p class=\"warn\">缓存已关闭（cache_enabled=false）。</p>"
            else:
                st = c.stats
                total = st["hits"] + st["misses"]
                rate = (st["hits"] / total * 100) if total else 0.0
                stats_html = (
                    "<table><tr><th>hits</th><th>misses</th><th>size</th><th>命中率</th></tr>"
                    f"<tr><td>{st['hits']}</td><td>{st['misses']}</td>"
                    f"<td>{st['size']}</td><td>{rate:.1f}%</td></tr></table>"
                )
            body = (
                "<div class=\"card\"><h1>缓存状态</h1>"
                "<table>"
                f"<tr><td>cache_enabled</td><td>{cfg.cache_enabled}</td></tr>"
                f"<tr><td>cache_ttl（秒）</td><td>{cfg.cache_ttl}</td></tr>"
                f"<tr><td>cache_max_size</td><td>{cfg.cache_max_size}</td></tr>"
                f"<tr><td>cache_path</td><td>{_html.escape(str(cfg.cache_path))}</td></tr>"
                "</table>"
                "<h2>实例统计（本面板进程内）</h2>"
                f"{stats_html}"
                "<p class=\"muted\">提示：公共 classify() 每次新建 Classifier 实例，缓存按实例生命周期生效；"
                "本长驻面板复用同一个 Classifier 实例，在首页重复提交相同 prompt 可观察到 hits 增长。</p>"
                "</div>"
            )
            self._html(200, _shell("缓存状态", body))

    return Handler


def create_app(config: RouterConfig | None = None):
    """构造处理器类与共享的 cfg/data/classifier（测试可直接复用）。"""
    cfg = config or load_config()
    data = load_data(cfg.data_dir, weights_override=cfg.weights_override)
    classifier = Classifier(data=data, cfg=cfg)
    handler_cls = _make_handler(cfg, data, classifier)
    return handler_cls, cfg, data, classifier


def run_server(host: str = "127.0.0.1", port: int = 10040, config: RouterConfig | None = None) -> None:
    """启动本地面板（仅监听 127.0.0.1，不暴露公网）。

    多线程（ThreadingHTTPServer）：分类调用较慢（默认 Qwen 约 20~30s）时
    不阻塞其它请求；缓存由 ClassifyCache 内部 RLock 保证线程安全。
    """
    handler_cls, cfg, data, classifier = create_app(config)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    print(f"cn-llm-router 本地面板已启动：http://{host}:{port}  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
