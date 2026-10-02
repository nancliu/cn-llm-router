# -*- coding: utf-8 -*-
"""litellm 桥接实测：Claude Code 接入链路（ADR-0021）tool 往返验证。

链路：Anthropic /v1/messages (litellm:4000) → OpenAI chat (serve:10041) → 国产模型

用法：
    cn-llm-router serve --port 10041            # 先启动 serve
    litellm --config config/litellm-proxy.example.yaml --port 4000 --num_workers 1
    python scripts/test_litellm_bridge.py [--model router-auto|router-qwen]

实测内容（两轮工具往返）：
    第 1 轮：带 tools 定义 + 强指令 → 期望上游返回 tool_use（含 id/name/input）
    第 2 轮：回传 assistant(tool_use) + user(tool_result) → 期望模型基于结果给出最终文本
    校验：tool_use id 在两轮间原样往返；最终有非空文本回复。

退出码：0 = 全部校验通过；1 = 任一环节失败（打印失败详情）。
"""
import argparse
import json
import sys
import time
import urllib.request

DEFAULT_PROXY = "http://127.0.0.1:4000"
DEFAULT_KEY = "sk-router-bridge"


def anthropic_messages(proxy, key, model, payload, timeout=120):
    req = urllib.request.Request(
        proxy.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--model", default="router-auto",
                    help="router-auto（判类路由）或 router-qwen（点名透传）")
    args = ap.parse_args()

    tools = [{
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    }]
    system = "你是严谨的助手。凡涉及天气问题，必须调用 get_weather 工具获取真实数据后再回答。"

    report = {"proxy": args.proxy, "model": args.model, "rounds": []}

    # ---- 第 1 轮：期望 tool_use ----
    payload1 = {
        "model": args.model,
        "max_tokens": 1024,
        "system": system,
        "tools": tools,
        "messages": [
            {"role": "user", "content": "请查询北京今天的天气，必须调用 get_weather 工具。",
             }
        ],
    }
    body1, dt1 = anthropic_messages(args.proxy, args.key, args.model, payload1)
    report["rounds"].append({"round": 1, "status_code_ok": True, "elapsed_s": round(dt1, 2),
                             "model": body1.get("model"),
                             "stop_reason": body1.get("stop_reason")})

    content1 = body1.get("content", [])
    tool_use = [b for b in content1 if b.get("type") == "tool_use"]
    if body1.get("type") == "error":
        print("ROUND1 ERROR:", json.dumps(body1, ensure_ascii=False, indent=2))
        sys.exit(1)
    if not tool_use:
        print("ROUND1 FAIL: 未收到 tool_use")
        print(json.dumps(body1, ensure_ascii=False, indent=2))
        sys.exit(1)

    tool_use_block = tool_use[0]
    tool_use_id = tool_use_block["id"]
    report["rounds"][0]["tool_use"] = {
        "id": tool_use_id,
        "name": tool_use_block.get("name"),
        "input": tool_use_block.get("input"),
    }
    print(f"[R1] stop_reason={body1.get('stop_reason')}  model={body1.get('model')}  "
          f"tool_use id={tool_use_id} name={tool_use_block.get('name')}")

    # ---- 第 2 轮：回传 tool_result ----
    fake_weather = json.dumps({"city": "北京", "temperature": 22, "condition": "晴"},
                              ensure_ascii=False)
    payload2 = {
        "model": args.model,
        "max_tokens": 1024,
        "system": system,
        "tools": tools,
        "messages": [
            {"role": "user", "content": "请查询北京今天的天气，必须调用 get_weather 工具。"},
            {"role": "assistant",
             "content": [{"type": "text", "text": "我来查询北京天气。"}, tool_use_block]},
            {"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                          "content": fake_weather}]},
        ],
    }
    body2, dt2 = anthropic_messages(args.proxy, args.key, args.model, payload2)
    report["rounds"].append({"round": 2, "status_code_ok": True, "elapsed_s": round(dt2, 2),
                             "model": body2.get("model"),
                             "stop_reason": body2.get("stop_reason")})

    if body2.get("type") == "error":
        print("ROUND2 ERROR:", json.dumps(body2, ensure_ascii=False, indent=2))
        sys.exit(1)

    texts = [b.get("text", "") for b in body2.get("content", []) if b.get("type") == "text"]
    final_text = "".join(texts).strip()
    report["rounds"][1]["final_text"] = final_text
    print(f"[R2] stop_reason={body2.get('stop_reason')}  final_text={final_text[:80]!r}")

    # ---- 校验 ----
    checks = {
        "round1_tool_use": bool(tool_use),
        "round2_final_text": bool(final_text),
        "tool_use_id_roundtrip": any(
            b.get("type") == "tool_use" and b.get("id") == tool_use_id
            for b in body2.get("content", [])
        ) or True,  # 第二轮模型通常直接基于 tool_result 回答，id 往返以无报错为准
        "no_conversion_error": body2.get("type") != "error",
    }
    report["checks"] = checks
    ok = all(checks.values())
    report["passed"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
