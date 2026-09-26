# ADR-0013：CI 两级评测门禁

- 状态：accepted
- 日期：2026-09-27
- 关联：ADR-0002（任务分类器）、ADR-0008（模型版本监控）、`scripts/eval_classifier.py`

## 背景

`scripts/eval_classifier.py` 已能对全量 golden cases（180 题，12 类 × 3 复杂度 × 5）跑在线评测并输出端到端准确率；2026-09-24 的基线见 `reports/eval-20260924.json`：

| 模型 | 端到端准确率 |
| --- | --- |
| Qwen3.8-Max-0902 | 99.4% |
| DeepSeek-V4.1-Flash-CED | 98.9% |
| 豆包Seed-2.1-Pro | 98.9% |

但这条链路此前只在人工发版前手动跑：分类 prompt、类别定义、规则兜底等任何改动一旦降低判类质量，只能靠人肉对比发现。需要把"判类质量不退化"变成一道 CI 门禁。

矛盾在于：在线评测必须消耗真实 LLM 额度、且依赖 API key，而 PR/主干 CI 不能要求每个贡献者都配 key。

## 决策

采用**两级门禁**：

1. **PR / 主干 CI（离线，默认）**：维持 `.github/workflows/ci.yml` 现有 `pytest` 矩阵（Python 3.10/3.11/3.12）不变。离线测试走规则兜底，全绿即合入。这是"结构与行为"门禁，不碰 LLM。
2. **Release tag（在线，可选）**：在 `.github/workflows/release.yml` 的 `build` 之后、`publish`（PyPI）之前新增 `eval-regression` job：
   - 检出源码、`pip install -e .`，把 `DASHSCOPE_API_KEY` / `VOLCENGINE_API_KEY` 两个 GitHub Secrets 注入环境；
   - shell 层判断：两个 key 都为空 → 打印"未配置 API key secrets，跳过全量评测回归（仅跑 pytest 离线门禁）"并正常退出 0；任一存在 → 运行 `python scripts/eval_classifier.py --check-baseline --output reports/eval-ci.json`，评测报告上传为 artifact；
   - 评测脚本 exit code 1 时 job 失败，`publish` 因 `needs: eval-regression` 不满足而被阻断。

## 基线与阈值口径

`scripts/eval_classifier.py` 新增常量（数字必须可溯源，见仓库约定）：

```python
# 来源：reports/eval-20260924.json（2026-09-24 全量 180 题人工评测）
EVAL_BASELINE = {
    "Qwen3.8-Max-0902": 0.994,
    "DeepSeek-V4.1-Flash-CED": 0.989,
    "豆包Seed-2.1-Pro": 0.989,
}
EVAL_THRESHOLD = 0.97  # 端到端准确率绝对下限，低于即拦截
```

`--check-baseline` 的判定规则：

- 对 `EVAL_BASELINE` 中每个模型：
  - 已配 key 且本次实际跑了 → 对比其实测端到端准确率与 `EVAL_THRESHOLD`（绝对下限，而非"必须追平基线"——基线模型本身有 1% 左右的正常波动空间）；低于阈值打印实测 vs 基线并置失败；
  - 未配 key → 明确打印"未配置 key，跳过：<模型>"，**不计失败、不误报**。
- `--models` 自定义跑出但不在基线表里的模型：照常出报告，不参与阈值判定。
- 发布前人工重跑评测后，把新数字回写 `EVAL_BASELINE` 常量并在注释里更新来源文件，禁止凭空编造。

## 权衡

- **只拦绝对下限、不拦"比上次低"**：180 题样本下逐题波动可达 ±0.5–1%，要求逐版本追平基线会误拦；97% 的绝对下限足以兜住"判类能力崩坏"级回归。
- **门禁放在 Release 而非 PR**：在线评测费钱费时间，PR 不应强制；tag 发布是质量闸门的天然位置——未配 secrets 的仓库 fork 也能照常 build，只是跳过在线门禁。
- **不把 secrets 写进 job-level `if`**：GitHub Actions 的 `secrets` 上下文在 job-level `if` 不可用，统一在 step 的 `env` 注入后用 shell `if` 判断。

## 影响

- `scripts/eval_classifier.py`：新增 `EVAL_BASELINE` / `EVAL_THRESHOLD` 常量与 `check_baseline()` 纯函数、`--check-baseline` flag；失败时 exit 1。
- `.github/workflows/release.yml`：新增 `eval-regression` job（`needs: build`，`publish` 改为 `needs: [build, eval-regression]`）。
- `.github/workflows/ci.yml`：不变。
- 新增 `tests/test_eval_baseline.py`：覆盖阈值拦截、缺 key 跳过、基线对比输出，不真实调用 LLM。
