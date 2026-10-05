"""本地面板测试（ADR-0017）。

在随机端口起 ThreadingHTTPServer（线程内 serve_forever），用 urllib.request 发请求。
cfg 来自临时空配置目录 + CN_LLM_ROUTER_NO_DOTENV=1（conftest），
classify 走确定性规则关键词兜底，不真实调 LLM。
"""
import dataclasses
import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cn_llm_router.config import load_config
from cn_llm_router.web import create_app


@pytest.fixture
def base_url(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg = load_config(config_dir=str(cfg_dir))
    # 把统计日志指到一个不存在的文件，便于 /cost 空数据用例
    cfg = dataclasses.replace(cfg, stats_log_path=str(tmp_path / "no_such.jsonl"))
    handler_cls, _, _, _ = create_app(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(url: str, form: dict) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


# 1. 首页 200 且含查询表单
def test_home_200_has_form(base_url):
    code, body = _get(base_url + "/")
    assert code == 200
    assert "<form" in body
    assert 'name="prompt"' in body
    assert "查询推荐" in body


# 2. POST /route 对含关键词 prompt 返回分类结果（离线规则兜底 → 程序编码）
def test_post_route_returns_classification(base_url):
    # 不传 availability_filter → 视为关闭，从全量集推荐（离线确定性）
    code, body = _post(base_url + "/route", {
        "prompt": "写一个Python函数解析JSON",
        "strategy": "平衡",
    })
    assert code == 200
    assert "程序编码" in body          # 规则关键词兜底命中
    assert "主选" in body             # 推荐结果渲染
    assert "备选" in body
    assert "国外主流模型参照" in body   # ADR-0019：推荐结果卡附国外参照
    assert "Claude Fable 5.1" in body


# 3. /cache 200 且含缓存配置字段
def test_cache_page_200(base_url):
    code, body = _get(base_url + "/cache")
    assert code == 200
    assert "cache_enabled" in body
    assert "cache_ttl" in body
    assert "hits" in body
    assert "misses" in body


# 4. /cost 无日志文件时 200 且提示无数据
def test_cost_no_data(base_url):
    code, body = _get(base_url + "/cost")
    assert code == 200
    assert "暂无统计数据" in body


# 5. 长驻 Classifier 复用：相同 prompt 第二次命中缓存（hits 增长）
def test_cache_hit_accumulates(base_url):
    form = {"prompt": "写一个Python函数解析JSON", "strategy": "平衡"}
    _post(base_url + "/route", form)
    _post(base_url + "/route", form)  # 第二次应命中缓存
    _, body = _get(base_url + "/cache")
    # hits 至少为 1（同进程长驻 classifier 累积）
    assert "hits" in body
    # 解析表格里的 hits 数值
    import re
    m = re.search(r"<td>(\d+)</td><td>(\d+)</td><td>(\d+)</td>", body)
    assert m is not None
    hits = int(m.group(1))
    assert hits >= 1


# 6. 多线程并发请求互不阻塞（ThreadingHTTPServer）：两个 POST 同时发出都应返回 200
def test_concurrent_posts_do_not_block(base_url):
    form = {"prompt": "写一个Python函数解析JSON", "strategy": "平衡"}
    results: list[tuple[int, str]] = []

    def do_post() -> None:
        results.append(_post(base_url + "/route", form))

    threads = [threading.Thread(target=do_post) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 4
    assert all(code == 200 for code, _ in results)
    assert all("程序编码" in body for _, body in results)


# 7. /routes 页 200：结构完整（网关状态 + 路由历史 + 评测），不依赖本机 serve 在线/离线
def test_routes_page_200(base_url):
    code, body = _get(base_url + "/routes")
    assert code == 200
    assert "网关路由" in body
    assert "网关状态" in body
    assert ("在线" in body) or ("离线" in body)  # serve 状态如实显示
    assert "分类评测" in body      # 180 golden cases 静态结论
    assert "100%" in body


# 8. 注册表渲染本机状态列（无 provider → 未配置 key；含 enabled 口径说明）
def test_registry_shows_provider_status(base_url):
    _, body = _get(base_url + "/")
    assert "本机状态" in body
    assert "未配置 key" in body
    assert "已停用" in body        # 口径说明文案
    assert "模型注册表" in body
