# 评测配置指南（key 清单与全量评测）

> 本文讲清楚：要跑 `scripts/eval_classifier.py` 的全量（多家）评测，需要哪些环境变量、去哪申请、怎么配、跑完怎么对比基线。
> 配 key 是用户侧操作；本仓库零密钥，所有 key 从环境变量读取（`.env` 已 gitignore）。

## 1. 先看当前 key 状态

```bash
python scripts/eval_classifier.py --list-ready
```

该命令**不加载 golden cases、不调用任何 LLM**，只打印 `config/providers.yaml`（缺省 `providers.example.yaml`）里全部模型的就绪状态，并打出未配 key 模型对应的 `key_env` 变量名。

## 2. 7 家 provider 的 key_env 清单

来源：`config/providers.example.yaml`（key_env 字段）+ `data/models.csv`（source_url 字段，可溯源）。

| Provider | key_env 环境变量 | 覆盖模型（providers.example.yaml） | key 获取入口 |
|---|---|---|---|
| 智谱 AI | `ZHIPU_API_KEY` | GLM-5.3-Flash / GLM-5.3 / GLM-5.2 | <https://bigmodel.cn/pricing>（控制台开放平台 → API Keys） |
| 深度求索 DeepSeek | `DEEPSEEK_API_KEY` | DeepSeek-V4-Pro-0813 / DeepSeek-V4-Flash-0731 | <https://api-docs.deepseek.com/zh-cn/quick_start/pricing/>（platform.deepseek.com → API keys） |
| 火山方舟 | `VOLCENGINE_API_KEY` | DeepSeek-V4.1-Flash-CED / 豆包Seed-2.1-Pro | <https://docs.volcengine.com/docs/82379/1544106>（console.volcengine.com/ark → API Key） |
| 阿里云百炼 | `DASHSCOPE_API_KEY` | Qwen3.8-Max-0902 | <https://help.aliyun.com/zh/model-studio/>（bailian.console.aliyun.com → API-KEY） |
| 月之暗面 Moonshot | `MOONSHOT_API_KEY` | Kimi-K3 / Kimi-K2.7-Code | <https://platform.kimi.com/>（Platform → API Key 管理） |
| 腾讯混元 | `TENCENT_HUNYUAN_API_KEY` | 腾讯混元Hy3 | <https://cloud.tencent.com/document/product/1823/130055>（console.cloud.tencent.com/hunyuan → API 密钥） |
| MiniMax | `MINIMAX_API_KEY` | MiniMax-M3 | <https://platform.minimaxi.com/docs/guides/pricing-paygo>（Platform → 接口 API Key） |

> 说明：
> - 火山方舟同时承载 DeepSeek-V4.1-Flash-CED 与豆包 Seed，共用一个 `VOLCENGINE_API_KEY`。
> - 腾讯混元 Hy4-preview 在 `data/models.csv` 中有记录，但**尚未在 `providers.example.yaml` 注册**，暂不参与评测；待补 provider 配置后纳入。
> - **讯飞星火 X2.5** 在 `data/models.csv` 中有评分记录（source_url：<https://static.xfyun.cn/doc/spark/TokenPlan.html>），但 `providers.example.yaml` 尚未配置其 provider 条目（base_url / api_model / key_env），**待补充 provider 配置**后才能被评测脚本调用。本文不臆造其 key_env。

## 3. .env 模板（可直接复制）

```bash
# 智谱 GLM
ZHIPU_API_KEY=
# 深度求索 DeepSeek
DEEPSEEK_API_KEY=
# 火山方舟（DeepSeek-V4.1 CED / 豆包 Seed）
VOLCENGINE_API_KEY=
# 阿里云百炼（Qwen）
DASHSCOPE_API_KEY=
# Kimi（月之暗面）
MOONSHOT_API_KEY=
# 腾讯混元
TENCENT_HUNYUAN_API_KEY=
# MiniMax
MINIMAX_API_KEY=
```

把上面内容追加到仓库根 `.env`（`cp .env.example .env` 后填入真实值）。代码启动时自动加载 `.env`（已存在的 shell 环境变量优先；`CN_LLM_ROUTER_NO_DOTENV=1` 可禁用）。

## 4. 跑全量评测

```bash
# 1) 确认就绪（可选但推荐）
python scripts/eval_classifier.py --list-ready

# 2) 指定要评的模型（逗号分隔；未配 key 的会明确打印"未配置 XXX，跳过"）
python scripts/eval_classifier.py \
  --models GLM-5.3-Flash,DeepSeek-V4-Pro-0813,Qwen3.8-Max-0902,Kimi-K3,腾讯混元Hy3,MiniMax-M3 \
  --output reports/eval-full.json

# 不带 --models 时，默认评 config/classifier.example.yaml 的候选链
python scripts/eval_classifier.py --output reports/eval-full.json
```

- 脚本逐模型跑全部 golden cases（180 题：12 类 × 3 复杂度 × 5 题），输出端到端准确率、LLM 直判准确率、按类别/复杂度矩阵、混淆矩阵，并按端到端准确率排序推荐默认分类模型。
- 未配 key 的模型**不会中断**，而是打印 `未配置 <key_env>，跳过 <model>` 后继续。
- 想先不花钱校验：`python scripts/eval_classifier.py --dry-run`。

## 5. 对比基线

基线见 `scripts/eval_classifier.py` 顶部的 `EVAL_BASELINE`（来源 `reports/eval-20260924.json`，2026-09-24 全量 180 题实测；数字可溯源，禁止编造）：

| 模型 | 端到端准确率基线 |
|---|---|
| Qwen3.8-Max-0902 | 99.4% |
| DeepSeek-V4.1-Flash-CED | 98.9% |
| 豆包Seed-2.1-Pro | 98.9% |

- **Release 门禁**：发布前加 `--check-baseline`，任一基线模型端到端准确率低于 97% 即 exit 1（ADR-0013）：
  ```bash
  python scripts/eval_classifier.py --check-baseline --output reports/eval-gate.json
  ```
- **与历史报告对比**：把新跑出的 `reports/eval-full.json` 与 `reports/eval-20260924.json` 按 `end_to_end_accuracy` 逐模型对比；新模型（智谱/DeepSeek 官方/Kimi/腾讯/MiniMax）首次评测无基线，记录到 `reports/` 后再补 `EVAL_BASELINE`。
- 改了分类提示词/默认模型链/权重后必须重跑全量评测，并把新数字连同来源日期写回 `EVAL_BASELINE` 或 ADR；缺口标"待补充"，不得拍脑袋填数。
