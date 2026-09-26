# ADR-0015：多模态（VLM）真实实测评分接入口径

- 状态：accepted
- 日期：2026-09-27
- 关联：ADR-0001（评分与选择策略）、ADR-0004（data/*.csv 为唯一真相源）、ADR-0008（版本监控）、ADR-0009（社区分叠加）

## 背景

当前 `data/scores.csv` 中"多模态理解"类的能力分，除少数模型（DeepSeek-V4.1-Flash-CED、Kimi-K3）有文本代理分/代理标注外，绝大多数模型该格为空——包括已注册且原生多模态的 Qwen3.8-Max-0902。即：选择器在多模态任务上**没有真实视觉输入能力信号**，要么用文本能力近似（不公平，文本强≠视觉强），要么该模型直接不参与排序。

已有 `scripts/eval_multimodal.py`（P2-b）：用标准库生成纯色 PNG、调用 OpenAI 兼容多模态 messages、关键词命中判分。2026-09-24 首跑结果（`reports/mm-eval-20260924.json`）：Qwen3.8-Max-0902 15/15=100%，DeepSeek-V4.1-Flash-CED 6/15=40%——证明链路可用，且两模型真实视觉能力差异巨大（与纯文本分数完全不同）。

需要把"真实 VLM 实测分"作为多模态类的权威信号接入 data 层与选择器。

## 决策

### 实测方法

`scripts/eval_multimodal.py` 扩展为更全面的 VLM 评测集，**≥20 题**，全部测试图仍用 Python 标准库（struct/zlib）生成，不依赖 PIL/外部图片：

| 题型 | 题目数 | 测什么 | 判定 |
| --- | --- | --- | --- |
| 颜色识别（纯色块） | 4 | 是否真的读到图像像素 | 期望颜色关键词命中 |
| 形状识别（白底实心圆/三角/矩形） | 3 | 基本图形感知 | 形状关键词命中 |
| 计数（2/3/5 个色块） | 3 | 数量感知 | 数字/中文数词命中 |
| OCR（5×7 点阵字渲染 "7"/"B"/"A3"） | 3 | 图中文字识别 | 字符大小写不敏感子串命中 |
| 颜色+形状组合题 | 4 | 联合理解（两族关键词须同时命中） | 多组关键词 AND |
| 按颜色计数 | 3 | 条件计数 | 颜色族 AND 数字族 |

共 **20 题**，按难度标注复杂度（低=纯色/OCR 单字符，中=形状/计数，高=组合题）。

- 评分：`VLM 实测分 = 该模型该复杂度桶的准确率 × 100`（0-100）；
- 判定：`expected_groups`（多族关键词）须**全部命中**才算对；单族即旧有关键词逻辑；
- 单题失败（API 报错/空回答）记错，不中断整模型。

### 数据落库

新增 `data/vlm_scores.csv`（ADR-0004 体系内新文件，**不动 scores.csv、不动飞书打分表**）：

| 列 | 含义 |
| --- | --- |
| `model` | 对应 `data/models.csv` 的 `logical_name` |
| `category` | 固定 `多模态理解` |
| `complexity` | `低`/`中`/`高`（按题桶聚合；某桶无题则不写行） |
| `score` | 实测准确率×100（0-100） |
| `basis` | 口径说明，如 `VLM真实实测; n=20; accuracy×100` |
| `num_questions` | 实测题目总数（同一模型所有桶共用） |
| `eval_date` | 评测日期 YYYY-MM-DD |
| `source_url` | 可溯源：脚本路径 + 实测 JSON 报告路径 + 评测日期 |
| `as_of` | 数据快照日期（= 评测日期） |

仅写入**真实跑过且 key 可用**的模型；无 key 模型（GLM-5.3-Flash 等）**不写行、不编造数字**，在本 ADR"待补充清单"列出。

### 选择器融合

`cn_llm_router/data_loader.py`：

- `RouterData` 新增 `vlm_scores: dict[(category, complexity, model) → float]`；
- 加载 `data/vlm_scores.csv`；**文件不存在则空 dict**（向后兼容，与 community_scores 同模式）；
- `_validate` 断言 vlm_scores 中的模型 ⊆ models。

`cn_llm_router/selector.py` 的 `_rank`：

1. 先取 `data.scores[(category, complexity, model)]`（文本代理分）；
2. 若 `category == "多模态理解"` 且该模型在 `vlm_scores` 中有分，则**以 VLM 实测分替代文本代理分**参与排序（VLM 实测是直接视觉证据，权重高于文本近似）；
3. 无 VLM 实测分的模型：维持文本代理分（无代理分的仍不参与排序），`select()` 的 notice 追加"该模型无 VLM 实测分，使用文本代理分"；
4. `ModelChoice.reason` 标注 `VLM 实测分` 或 `文本代理分`；
5. VLM 实测分**不再叠加** ADR-0009 的 LMArena 社区分（实测分本身即直接测量，避免二次混入）；非多模态类行为完全不变。

## 待补充清单（无 key / 未实测，禁止臆造）

- `GLM-5.3-Flash`（原生多模态，但本机未配 `ZHIPU_API_KEY`）；
- `豆包Seed-2.1-Pro`（火山套餐端点图像输入支持未验证，本轮未纳入实测）；
- `Kimi-K3`、`MiniMax-M3`、`腾讯混元Hy4-preview`（无对应厂商 key）；
- 纯文本模型（DeepSeek-V4-Pro/Flash-0731、GLM-5.2/5.3、Kimi-K2.7-Code、讯飞星火X2.5 等）：视觉能力天然 N/A，维持现状不补。

后续配 key 后重跑 `eval_multimodal.py --output-csv data/vlm_scores.csv` 按同口径追加。

## 不做什么

- **不修改** `data/scores.csv`（评分矩阵刷新需重新评测，另行立项）；
- **不动飞书打分表**（data 层独立 csv 足够；飞书同步由 `sync_from_lark.py` 单向管理）；
- **不编造分数**：未实测模型不写行；
- **不改** classify/route/orchestrate 公开签名；
- 不做视频/文档 OCR 等更重场景（留 backlog）。

## 影响

- `scripts/eval_multimodal.py`：场景集 15 → 20 题（颜色/形状/计数/OCR/组合），新增 `--output-csv` 直出入库格式；
- 新增 `data/vlm_scores.csv`；
- `cn_llm_router/data_loader.py`：`RouterData.vlm_scores` + 校验；
- `cn_llm_router/selector.py`：mm 类实测分优先、reason/notice 标注；
- 新增 `tests/test_vlm_scores.py`。
