"""火山方舟标准 API（普通 apikey）测试：key 校验 + 模型列表 + 经网关真实对话。

用法：python scripts/test_ark_api.py [--skip-list]
说明：走 cn_llm_router.gateway.build_client 实际调用，验证 config → key → base_url → api_model 全链路。
"""
from __future__ import annotations

import sys

from cn_llm_router.config import load_config

SKIP_LIST = "--skip-list" in sys.argv

cfg = load_config()
for logical in ("DeepSeek-V4.1-Flash-CED-ARK", "豆包Seed-2.1-Pro-ARK"):
    prov = cfg.providers[logical]
    print(f"\n===== {logical} =====")
    print(f"  provider={prov.provider}  base_url={prov.base_url}")
    print(f"  api_model={prov.api_model}  key_env={prov.key_env}")

    if not SKIP_LIST:
        import httpx
        from cn_llm_router.config import _load_dotenv

        _load_dotenv()
        import os

        key = os.environ.get(prov.key_env, "")
        r = httpx.get(prov.base_url.rstrip("/") + "/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=30)
        print(f"  [GET /models] status={r.status_code}")
        if r.status_code != 200:
            print("  ", r.text[:500])

    from cn_llm_router.gateway import build_client

    client = build_client(prov)
    resp = client.chat.completions.create(
        model=prov.api_model,
        messages=[{"role": "user", "content": "请只回复两个字：收到"}],
        max_tokens=512,  # 推理型模型思考链会先消耗 token，过小会返回空 content
    )
    text = resp.choices[0].message.content or ""
    usage = resp.usage
    print(f"  [completion] 回复={text!r}")
    print(f"  [completion] usage prompt={usage.prompt_tokens} completion={usage.completion_tokens}")

print("\n全部通过：标准 API key 可经网关正常调用。")
