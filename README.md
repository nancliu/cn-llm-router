# cn-llm-router

国内大模型选择器：识别任务类别（12 类）与复杂度（3 级），依据公开评测合成的评分矩阵与性价比策略，推荐并路由到最合适的国产大模型（DeepSeek / Qwen / Kimi / GLM / 豆包 / 混元 / 讯飞 / MiniMax 等）。

## 状态

- 已完成：打分表 v1-20260923（12 任务类别 × 3 复杂度 × 15 模型，含三档性价比策略：纯能力优先 / 平衡 / 性价比优先）
- 开发中：路由层（任务分类器 + 模型选择器 + 网关接入）

## 文档

- `CONTEXT.md` — 项目上下文与词汇表
- `docs/adr/` — 架构决策记录
- `docs/agents/` — agent 工作约定（issue tracker / triage / domain）

## License

见 [LICENSE](./LICENSE)
