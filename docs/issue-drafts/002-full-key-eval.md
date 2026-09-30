---
name: 功能请求
about: 补齐 5 家厂商 key 的全量分类评测（开放协作任务）
title: "[Feature] 补齐 5 家厂商 key 的全量分类评测（协作）"
labels: ["needs-triage", "good-first-issue"]
---

## 动机

当前在线评测（`scripts/eval_classifier.py`，180 golden cases，12 类 × 3 复杂度 × 5 题）仅覆盖 2 家已配 key 的厂商：

- 火山方舟 coding-plan：`DeepSeek-V4.1-Flash-CED`、`豆包Seed-2.1-Pro`
- 阿里云百炼 token-plan：`Qwen3.8-Max-0902`

其余 5 家厂商的 9 个模型（智谱 GLM-5.3/GLM-5.3-Flash/GLM-5.2、月之暗面 Kimi-K3/Kimi-K2.7-Code、DeepSeek 官方 V4-Pro/V4-Flash、MiniMax-M3、腾讯混元 Hy3）因未配置 API key 无法参与全量评测，ADR-0013 发布门禁（EVAL_THRESHOLD=0.97）对它们只能"跳过"而非实测验证。

## 期望方案

任何持有上述任一厂商 API key 的贡献者，本地按 `docs/eval-setup.md` 配置 `.env` 后运行：

```bash
python scripts/eval_classifier.py --models GLM-5.3,GLM-5.3-Flash,Kimi-K3,MiniMax-M3,腾讯混元Hy3,DeepSeek-V4-Pro-0813 --output reports/eval-<你的标识>-<ts>.json
```

提交内容（PR）：

1. `reports/eval-*.json` 评测结果（含端到端准确率 / LLM 直判 / 复杂度命中 / 混淆矩阵）
2. 若有模型端到端准确率 ≥ 0.97，同步更新 `scripts/eval_classifier.py` 的 `EVAL_BASELINE`（数字须与报告一致）
3. 无 key 也可只提交公开来源的基准成绩（必须带 source_url，缺口标"待补充"，禁止编造）

## 备选方案

- 由维护者自费配齐 5 家 key 评测——成本高、key 生命周期管理繁琐，不适合开源维护；更倾向社区协作分摊。
- 纯离线不评测——无法获得真实判类准确率，不符合 ADR-0013 门禁初衷。

## 影响面

- 是否涉及路由/分类/选择器核心逻辑：否
- 是否需要新增/修改评分数据：否（评测产出更新的是 `EVAL_BASELINE` 与报告）
- 是否需要写 ADR：否（评测流程与门禁已有 ADR-0013 覆盖；新增模型评测口径沿用现有 180 题基线）

## 补充信息

- 评测脚本用法：`python scripts/eval_classifier.py --list-ready`（查看已配/未配 key）
- key 获取入口与 `.env` 模板：`docs/eval-setup.md`
- 现有基线（ADR-0013，来源 `reports/eval-20260924.json`）：Qwen3.8-Max-0902 0.994 / DeepSeek-V4.1-Flash-CED 0.989 / 豆包Seed-2.1-Pro 0.989
- golden cases：`tests/fixtures/golden_cases.yaml`（180 题，含 `rule_testable` 标注）
