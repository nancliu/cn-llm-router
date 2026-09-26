# 贡献指南

欢迎参与 cn-llm-router。本文件说明开发环境、协作流程与数据贡献规范。 issues 用 GitHub issues 跟踪（见 `docs/agents/issue-tracker.md`），triage 标签见 `docs/agents/triage-labels.md`。

## 1. 开发环境搭建

```bash
git clone https://github.com/<org>/cn-llm-router.git
cd cn-llm-router

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -e ".[dev]"     # 含 pytest、build
# 可选：LiteLLM backend 支持
# pip install -e ".[litellm]"

cp .env.example .env        # 评测/路由需要 key；纯开发可跳过

pytest -q                    # 跑全量测试
```

要求：Python 3.10 / 3.11 / 3.12（CI 三矩阵）。`classify` / `select` / 离线编排**不需要任何 key**，clone 即可开发与测试。

## 2. ADR 流程

影响**架构或评分口径**的改动，先写 ADR 再实现（见 `docs/adr/`，现有 0001–0013）：

1. 复制 `docs/adr/0000-template` 风格新建 `NNNN-<kebab-title>.md`（编号接最大）；
2. 在 ADR 里写清背景、决策、后果；涉及评分/价格数字的，**把来源 URL 写进口径区**；
3. 提 PR 时把 ADR 与实现一起评审；纯文档/ typo / 示例修复不必写 ADR。

## 3. 代码与文档规范

- 文档默认**简体中文**；代码标识符（变量/函数/类）用**英文**。
- **任何评分/价格数字必须可溯源**：来源 URL 写入 ADR 或数据文件的 `source_url`/`as_of` 列；禁止编造评测数字，缺口标"待补充"。
- 仓库零密钥：key 一律从环境变量读，`.env` 已 gitignore，不得把真实 key 提交进仓库。
- 不静默失败：分类降级、可用性过滤等行为要打 `low_confidence`/`fallback_reason`/notice。
- 公共 API 变更同步更新 `README.md`（及 `README.en.md`）与相关 docs。

## 4. 提交与 PR 流程

- 分支：从 `main` 切出 `<type>/<short-desc>` 分支（如 `feat/orchestrator-guide`、`fix/eval-skip`）；
- commit message  conventional 风格前缀（`feat:` / `fix:` / `docs:` / `refactor:` / `test:`）；
- 每个 PR 必须本地 `pytest -q` 通过；CI（`.github/workflows/ci.yml`）会在 3.10/3.11/3.12 三版本上重跑 `pytest tests -q`，全绿才可合并；
- 发布门禁：改了分类器/默认模型链时，用 `scripts/eval_classifier.py --check-baseline` 验证不低于阈值（见 ADR-0013）。

## 5. 评分数据贡献规范

`data/*.csv` 是评分矩阵唯一真相源（ADR-0004）。新增/更新评分必须遵守：

| 文件 | 列 | 说明 |
|---|---|---|
| `data/scores.csv` | `category,complexity,model,score,basis,as_of` | 12 类 × 3 复杂度 × 模型的能力分（SuperCLUE 口径） |
| `data/community_scores.csv` | `model,source,metric,value,source_url,as_of` | 社区榜单叠加分（ADR-0009，如 lmarena ELO） |
| `data/vlm_scores.csv` | 待补充（多模态评分，schema 立项时在 ADR 中定义） | 多模态模型评分叠加；尚未建立，贡献前先开 issue |

硬性要求：

1. **必须带 `source_url`**（公开榜单/官方文档 URL）与 `as_of`（数据抓取日期 `YYYY-MM-DD`）；
2. **禁止编造数字**：没有可靠来源的格子留空并在 `basis` 或 issue 里标"待补充"，不要拍脑袋填分；
3. 新模型入表：先在 `data/models.csv` 加行（含 `source_url`、`as_of`、价格），再补 `scores.csv` / `community_scores.csv`；
4. 修正已有错误数据：用 `.github/ISSUE_TEMPLATE/data_correction.md` 开 issue，附来源 URL，经复核后改 CSV；
5. 飞书打分表同步用 `scripts/sync_from_lark.py`，不要手改已同步的列。

感谢你的贡献。
