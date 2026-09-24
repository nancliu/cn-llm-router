# ADR-0009：社区/人工评测叠加（LMArena Elo 第二数据源）

- 状态：accepted
- 日期：2026-09-24
- 关联：ADR-0001（评分与选择策略）、ADR-0004（data/*.csv 为唯一真相源）

## 背景

v1 的能力分仅来自两个渠道：

1. **SuperCLUE** 官方榜单（按 category × complexity × model 打分，0-100）；
2. **Terminal 代理实测**补充的少量分数。

两者都是"任务级、按类别细分"的评测，维度单一：既缺少独立第三方的**模型级通用能力**信号，也无法交叉验证 SuperCLUE 打分是否与社区盲测口碑一致。当两个模型在某一任务格上分数接近时，路由缺乏一个不依赖 SuperCLUE 的外部锚点。

需要接入一个第二数据源作为评分**补充维度**，但**不能**动摇 SuperCLUE 作为主数据源的地位。

## 决策

接入 **LMArena（arena.ai，原 Chatbot Arena）文本总榜 Elo** 作为**模型级通用能力信号**，与任务级 SuperCLUE 分加权融合后用于排序。

- LMArena Elo 是模型级整体分（不按任务类别细分），范围约 1300-1500（本快照极值 952-1506）；
- 加载时线性归一化到 0-100，再与 SuperCLUE 分融合；
- 融合分仅用于排序与 `ModelChoice.score` 展示，**不回写** `data/scores.csv`。

## 数据口径

新增 `data/community_scores.csv`（ADR-0004 唯一真相源体系内的新文件），列：

| 列 | 含义 |
| --- | --- |
| `model` | 对应 `data/models.csv` 的 `logical_name` |
| `source` | 数据源标识，如 `lmarena` |
| `metric` | 指标名，如 `elo_overall` |
| `value` | 原始指标值（Elo 原值，不归一化） |
| `source_url` | 可溯源 URL（必填，禁止空） |
| `as_of` | 数据快照日期（YYYY-MM-DD） |

仅收录**可溯源**数据：每条必须有真实 `source_url` 与 `as_of`；抓不到/对不上号的模型**不写入**，在本 ADR"待补充清单"中列出。**禁止编造 Elo 数字。**

## 融合公式

对同时有 SuperCLUE 任务分与社区分的模型：

```
blended = α × superclue_score + β × normalized_community_score
```

- `α` 默认 **0.7**，`β` 默认 **0.3**（社区分是补充信号，权重低于主数据源）；
- α/β 可通过 `config/selector.yaml` 的 `community_alpha` / `community_beta` 覆盖；
- **无社区分的模型 β=0**，即纯 SuperCLUE 分（不报错，仅 notice 提示）；
- `community_scores.csv` 不存在或为空时，行为与 v1 完全一致（向后兼容）。

`blended` 仅用于排序；`ModelChoice.score` 记录 blended 值，`reason` 中说明融合比例。

## 归一化

Elo 线性映射到 0-100：

```
score_0_100 = (elo - ELO_MIN) / (ELO_MAX - ELO_MIN) × 100
```

- `ELO_MIN = 1300` → 0 分；`ELO_MAX = 1550` → 100 分；
- 结果 clamp 到 `[0, 100]`（超出区间的 Elo 截断，不外溢）；
- 映射区间为代码内常量（v1 不暴露到 yaml，保持配置面最小）。

## 已收录数据（2026-09-13 LMArena 文本总榜快照）

来源：LMArena 静态镜像 https://lmarena-ai-chatbot-arena.static.hf.space/index.html （快照标注 Sep 13, 2026，8,146,274 票，402 模型；主站 https://arena.ai/leaderboard/text ）。

| models.csv logical_name | LMArena slug | Elo | 归一化(0-100) |
| --- | --- | --- | --- |
| Kimi-K3 | kimi-k3-max | 1485±5 | 74.0 |
| GLM-5.3 | glm-5.3-max | 1483±6 | 73.2 |
| Qwen3.8-Max-0902 | qwen3.8-max | 1481±6 | 72.4 |
| GLM-5.3-Flash | glm-5.3-flash | 1475±7 | 70.0 |
| GLM-5.2 | glm-5.2-max | 1472±5 | 68.8 |
| DeepSeek-V4-Pro-0813 | deepseek-v4-pro-high-20260813 | 1463±7 | 65.2 |
| MiniMax-M3 | minimax-m3 | 1441±4 | 56.4 |
| DeepSeek-V4-Flash-0731 | deepseek-v4-flash | 1436±4 | 54.4 |
| 腾讯混元Hy3 | hunyuan-hy3-preview | 1413±8 | 45.2 |

映射说明：

- `DeepSeek-V4-Pro-0813` 对应 LMArena 带日期后缀的 `deepseek-v4-pro-high-20260813`（日期 20260813 与 models.csv 版本 2026-08-13 一致）；
- `Kimi-K3` 对应 `kimi-k3-max`（榜单无裸 `kimi-k3`，max 为月之暗面旗舰档）；
- `GLM-5.2` / `GLM-5.3` 对应榜单 `glm-5.2-max` / `glm-5.3-max`。

## 待补充清单（v1 未收录，禁止臆造）

以下模型在 2026-09-13 快照中**找不到可对应的 LMArena 条目**，community_scores.csv 不写入，β=0 纯 SuperCLUE：

- `DeepSeek-V4.1-Flash-CED`（榜单未见 v4.1 条目）
- `Kimi-K2.7-Code`（榜单无 k2.7-code；k2.6/k2.5 非同代型号，不强行映射）
- `豆包Seed-2.1-Pro`（榜单未见 doubao/seed 条目）
- `Seedream-5.0`（图像生成模型，不在文本总榜，天然 N/A）
- `腾讯混元Hy4-preview`（榜单仅见 hunyuan-hy3-preview，无 hy4）
- `讯飞星火X2.5`（榜单未见 spark/xinghuo 条目）

后续若这些模型上榜或有其他可溯源社区分（如 SuperCLUE-VLM 多模态专项分），再按同一 CSV 口径追加。

## 不做什么

- **不替换** SuperCLUE 为主数据源：α=0.7 保证任务级分主导；
- **不按类别细分社区分**：v1 仅模型级整体 Elo，不做 category×complexity 拆分；
- **不自动抓取/自动更新** community_scores.csv：与 ADR-0008 一致，榜单数字由人工核实后入库（本 ADR 即人工核实结果）；
- **不修改** `data/scores.csv` / `data/models.csv` / `data/categories.yaml`；
- **不改** classify / route / orchestrate 的签名。

## 影响

- 新增 `data/community_scores.csv`；
- `cn_llm_router/data_loader.py`：`RouterData` 新增 `community_scores`（归一化 0-100）与 `community_raw`（原值+来源）；文件缺失时为空 dict（向后兼容）；
- `cn_llm_router/config.py`：`RouterConfig` 新增 `community_alpha=0.7` / `community_beta=0.3`，可由 `config/selector.yaml` 覆盖；
- `cn_llm_router/selector.py`：`_rank` 取分后、排序前插入融合；无社区分模型 notice 提示；
- 新增 `tests/test_community_scores.py`。
