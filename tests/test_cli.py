"""CLI 测试：python -m cn_llm_router 与注册入口 cn-llm-router 的冒烟覆盖。"""
import json
import os
import subprocess
import sys

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent


def run_cli(*args, cwd=None, env=None):
    # 继承父进程环境，仅加隔离开关（子进程不自动加载本地 .env）
    full_env = dict(os.environ)
    full_env["CN_LLM_ROUTER_NO_DOTENV"] = "1"
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "cn_llm_router", *args],
        capture_output=True, text=True, cwd=cwd or REPO, env=full_env,
    )


def test_classify_text():
    r = run_cli("classify", "帮我写一个Python函数解析JSON")
    assert r.returncode == 0, r.stderr
    assert "程序编码" in r.stdout


def test_classify_json():
    r = run_cli("classify", "--json", "帮我写一个Python函数解析JSON")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["category"] == "程序编码"


def test_select_offline():
    r = run_cli("select", "--category", "程序编码", "--complexity", "低",
                "--strategy", "平衡", "--no-availability-filter")
    assert r.returncode == 0, r.stderr
    assert "主选" in r.stdout


def test_select_no_key_raises_hint(monkeypatch):
    # 默认开可用性过滤、无 key → 报错并带 availability_filter 提示
    monkeypatch.delenv("VOLCENGINE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    r = run_cli("select", "--category", "程序编码", "--complexity", "低")
    assert r.returncode == 2
    assert "availability_filter" in r.stderr


def test_select_json():
    r = run_cli("select", "--json", "--category", "数据分析", "--complexity", "中",
                "--strategy", "性价比优先", "--no-availability-filter")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["primary"]["logical_name"]
    assert out["backup"]["logical_name"]
    assert out["strategy"] == "性价比优先"


def test_route_offline():
    r = run_cli("route", "--no-availability-filter", "写一个Python函数解析JSON")
    assert r.returncode == 0, r.stderr
    assert "客户端就绪" in r.stdout


def test_list_models():
    r = run_cli("list-models")
    assert r.returncode == 0, r.stderr
    assert "DeepSeek" in r.stdout


def test_list_models_json_count():
    r = run_cli("list-models", "--json")
    out = json.loads(r.stdout)
    assert len(out) == 15


def test_list_categories():
    r = run_cli("list-categories", "--json")
    out = json.loads(r.stdout)
    assert len(out) == 12
    names = {c["name"] for c in out}
    assert names == {"程序编码", "前端UX设计", "文书撰写", "数据分析", "数学推理", "Agent编排",
                     "长文档处理", "翻译润色", "多模态理解", "知识问答/检索", "创意生成", "代码审查与调试"}


def test_list_strategies():
    r = run_cli("list-strategies", "--json")
    out = json.loads(r.stdout)
    assert {s["strategy"] for s in out} == {"纯能力优先", "平衡", "性价比优先"}


def test_bad_complexity_rejected():
    r = run_cli("select", "--category", "程序编码", "--complexity", "超高")
    assert r.returncode == 2
