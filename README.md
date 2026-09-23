# cn-llm-router

国内大模型选择器：识别任务类别（12 类）与复杂度（3 级），依据公开评测合成的评分矩阵与性价比策略，推荐并路由到最合适的国产大模型（DeepSeek / Qwen / Kimi / GLM / 豆包 / 混元 / 讯飞 / MiniMax 等）。

## 状态

- ✅ 打分表 v1-20260923（12 任务类别 × 3 复杂度 × 15 模型，含三档性价比策略：纯能力优先 / 平衡 / 性价比优先）
- ✅ 路由层 v1（任务分类器 + 模型选择器 + OpenAI 兼容薄网关），47 个离线测试通过（Python 3.10/3.11/3.12，GitHub Actions CI）
- ⏳ backlog：飞书同步脚本、分类准确率在线评测、CLI 工具、PyPI 发布、LiteLLM provider

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

## 文档

- `CONTEXT.md` — 项目上下文与词汇表
- `docs/adr/` — 架构决策记录（0001 评分口径 / 0002 分类器 / 0003 网关 / 0004 数据源 / 0005 可用性过滤）
- `docs/spec/router-v1.md` — 路由层 v1 规格
- `docs/agents/` — agent 工作约定（issue tracker / triage / domain）

## License

见 [LICENSE](./LICENSE)
