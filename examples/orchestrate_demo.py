#!/usr/bin/env python3
"""Sub-Agent 多模型编排离线示例（P2-6，对应 ADR-0007）。

直接运行：
    python examples/orchestrate_demo.py

本示例**不调用任何 LLM、不依赖 API key**：
- 无 key 时分类器自动走"规则关键词兜底 → 默认值兜底"（见 cn_llm_router/classifier.py），
  所以下面三条子任务描述里都带了可命中规则表的关键词；
- availability_filter=False：从全量模型集中确定性推荐（ADR-0005），不按 key 可用性过滤；
- RouterClient 是**懒加载**的——这里只打印每个主选模型"是否已配 key"，
  真正发请求需要对应环境变量（见 .env.example）。

配好 key 后，把文件末尾"实际调用"注释打开即可用 client.chat.completions.create(...) 发请求。
"""
from __future__ import annotations

import dataclasses
import os
import sys

# 允许从仓库根直接运行：python examples/orchestrate_demo.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cn_llm_router import SubTaskSpec, load_config, orchestrate  # noqa: E402
from cn_llm_router.config import key_available  # noqa: E402


def readiness(route, cfg) -> str:
    """打印主选模型的 key 就绪状态（不实际调用上游）。"""
    name = route.recommendation.primary.logical_name
    prov = cfg.providers.get(name)
    if prov is None:
        return f"{name}：未在 config/providers.yaml 中配置 provider"
    if key_available(prov):
        return f"{name}：key 已配置（{prov.key_env}），可直接发请求"
    return f"{name}：key 未配置（需环境变量 {prov.key_env}）——离线演示仅看推荐"


def main() -> None:
    subtasks = [
        SubTaskSpec(
            id="coding",
            description="写一个 Python 脚本，读取 CSV 并按日期汇总订单量，要求带命令行参数",
            strategy="纯能力优先",   # 编码要质量
        ),
        SubTaskSpec(
            id="doc",
            description="把本周进展写成一份对内周报，包含完成事项、风险与下周计划",
            strategy="性价比优先",   # 文书模板化，省成本
        ),
        SubTaskSpec(
            id="data",
            description="分析一下近半年销售数据的月度趋势，找出波动最大的品类并给出口径",
            strategy="平衡",
        ),
    ]

    # 关键：把分类模型链置空 → Classifier._try_llm 直接返回 None，
    # 强制走"规则关键词兜底"，无论本机是否配了 key 都不会发任何网络请求。
    # （配好 key、想看 LLM 判类时，删掉这行 replace、改用 config=None 即可。）
    cfg = dataclasses.replace(load_config(), classifier_models=[])

    result = orchestrate(
        subtasks,
        strategy="平衡",             # 全局默认策略；子任务级 strategy 可覆盖
        availability_filter=False,   # 离线演示：从全量集推荐，不要求 key
        config=cfg,
    )

    print("=" * 68)
    print(f"编排完成：{len(result.subtasks)} 个子任务 | 全局策略={result.strategy} "
          f"| availability_filter={result.availability_filtered}")
    print("=" * 68)

    # orchestrate 内部所有子任务共享同一份 cfg/client 配置
    cfg = result.subtasks[0].client.cfg

    for r in result.subtasks:
        clf = r.classification
        rec = r.recommendation
        print(f"\n[{r.spec.id}] {r.spec.description}")
        print(f"  判类: {clf.category} / 复杂度={clf.complexity}"
              f"（low_confidence={clf.low_confidence}，"
              f"{clf.fallback_reason or 'LLM 判类'}）")
        print(f"  生效策略: {rec.strategy}")
        print(f"  主选模型: {rec.primary.logical_name}（{rec.primary.vendor}，"
              f"score={rec.primary.score}）")
        if rec.backup:
            print(f"  备选模型: {rec.backup.logical_name}（{rec.backup.vendor}）"
                  f"——主选网络/5xx/429 失败时自动切换")
        print(f"  推荐理由: {rec.primary.reason}")
        print(f"  Client 就绪: {readiness(r, cfg)}")
        for note in rec.notice:
            print(f"  备注: {note}")

    print("\n" + "=" * 68)
    print("配好 key 后，实际调用（取消注释即可）：")
    print('''
    from cn_llm_router import SubTaskSpec, orchestrate

    result = orchestrate([...], availability_filter=True)   # 有 key 时开可用性过滤
    for r in result.subtasks:
        resp = r.client.chat.completions.create(
            model=r.recommendation.primary.logical_name,
            messages=[{"role": "user", "content": r.spec.description}],
        )
        print(r.spec.id, "->", resp.choices[0].message.content)
''')


if __name__ == "__main__":
    main()
