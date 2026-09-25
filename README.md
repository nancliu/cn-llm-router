# cn-llm-router

国内大模型选择器：识别任务类别（12 类）与复杂度（3 级），依据公开评测合成的评分矩阵与性价比策略，推荐并路由到最合适的国产大模型（DeepSeek / Qwen / Kimi / GLM / 豆包 / 混元 / 讯飞 / MiniMax 等）。

## 状态

- ✅ 打分表 v1-20260923（12 任务类别 × 3 复杂度 × 15 模型，含三档性价比策略：纯能力优先 / 平衡 / 性价比优先）
- ✅ 路由层 v1（任务分类器 + 模型选择器 + OpenAI 兼容薄网关），101 个测试通过（Python 3.10/3.11/3.12，GitHub Actions CI）
- ✅ Sub-Agent 多模型编排（ADR-0007）：一个任务拆多个子任务，每个独立判类选模型分配不同大模型
- ✅ 工具链：飞书打分表同步、分类在线评测（180 golden cases）、多模态实测、模型版本跟踪（ADR-0008）、社区评测叠加（ADR-0009）、CLI、PyPI 打包（wheel 已验证）、LiteLLM backend
- ✅ 在线评测（2026-09-24，火山 coding-plan / 百炼 token-plan 实测，180 golden cases）：Qwen3.8-Max-0902 端到端 99.4% 为默认分类模型；DeepSeek-V4.1-Flash-CED 98.9% 且快约 6 倍（报告见 `reports/eval-20260924.json`）

## 快速使用

```bash
pip install -e ".[dev]"
# 离线可用（无需 key）：分类 + 推荐（availability_filter=False 从全量集比较）
python - <<'PY'
from cn_llm_router import classify, select, route

# 1) 任务分类（LLM 判类；未配 key 时走规则/默认值兜底，标 low_confidence）
print(classify("帮我写一个Python函数解析JSON"))

# 2) 模型推荐（确定性，data/*.csv 为唯一数据源）
rec = select("程序编码", "低", strategy="平衡", availability_filter=False)
print(rec.primary.logical_name, rec.backup.logical_name, rec.notice)

# 3) 路由：拿到 OpenAI 兼容客户端（失败自动切备选）——需配置 API key
cp .env.example .env   # 填入各厂商 key（如 ZHIPU_API_KEY）
result = route("写一个Python函数解析JSON", strategy="平衡")
completion = result.client.chat.completions.create(
    model=result.recommendation.primary.logical_name,
    messages=[{"role": "user", "content": "…"}],
)
PY
```

三档策略：`纯能力优先` / `平衡`（默认）/ `性价比优先`；默认按已配置 key 过滤推荐（`config/selector.yaml` 中 `availability_filter: false` 关闭，从全量集比较）。

## CLI

```bash
pip install -e ".[dev]"   # 注册 cn-llm-router 命令；或 python -m cn_llm_router
cn-llm-router classify "帮我写一个Python函数解析JSON"        # 任务分类
cn-llm-router select --category 程序编码 --complexity 低 --no-availability-filter   # 模型推荐
cn-llm-router route "用SQL统计每日订单量" --no-availability-filter                 # 分类+推荐+就绪客户端
cn-llm-router list-models / list-categories / list-strategies                      # 数据与策略查看
# 全部命令支持 --json（stdout 仅一份 JSON，可管道/脚本化）
```

## 工具链（scripts/）

| 脚本 | 用途 |
|---|---|
| `scripts/sync_from_lark.py --url <打分表URL>` | 飞书打分表 → `data/*.csv` 同步（幂等：内容一致不重写；需 `lark-cli`；URL 也可放环境变量 `CN_LLM_ROUTER_SHEET_URL`） |
| `scripts/eval_classifier.py [--models A,B] [--dry-run]` | LLM 判类在线评测：180 golden cases（12 类 × 3 复杂度 × 5 题），输出端到端/LLM 直判准确率、按类别矩阵、混淆矩阵，推荐默认分类模型。需至少一个分类模型的 API key |
| `scripts/export_data.py <快照.json>` | 一次性导出（sync 脚本内部复用） |

## 网关 backend

`config/providers.yaml` 中每个 provider 可选 `backend`（默认 `openai`）：

- `openai`：OpenAI 兼容端点直连（base_url + api_key）
- `litellm`：经 LiteLLM 直连（`provider/api_model` 组合路由、API 统一），需 `pip install "cn-llm-router[litellm]"`

失败自动切备选（仅网络/5xx/429 类错误；401 等鉴权错误直抛），可用 `config/selector.yaml` 的 `max_failover` 调整或关闭。

## 文档

- `CONTEXT.md` — 项目上下文与词汇表
- `docs/adr/` — 架构决策记录（0001 评分口径 / 0002 分类器 / 0003 网关 / 0004 数据源 / 0005 可用性过滤）
- `docs/spec/router-v1.md` — 路由层 v1 规格
- `docs/agents/` — agent 工作约定（issue tracker / triage / domain）

## License

见 [LICENSE](./LICENSE)
