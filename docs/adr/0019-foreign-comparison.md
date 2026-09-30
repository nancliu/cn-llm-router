# ADR-0019：国内外主流模型对比（参考展示，不参与路由）

- 状态：accepted
- 日期：2026-09-30
- 关联：ADR-0001（成本口径）、ADR-0004（数据源）、ADR-0009（社区评分）、ADR-0018（双模型分流）

## 背景

用户要求建立与国外主流模型（Claude / GPT / Grok / Gemini）的对比，为使用国产模型提供信心。
已确认决策：数据用公开榜单（SuperCLUE / LMArena 第三方快照 / 厂商官方定价页），
真实 API 实测留给开源社区（无国外 key，且与"5 家国内 key 评测转交社区"一致）。

## 决策

1. **数据资产**：新增 `data/foreign_comparison.csv`（11 模型 = 6 国内头部 + 5 国外旗舰），
   字段含 SuperCLUE 总分 / LMArena Elo / 输入输出单价 / 上下文 / 开源 / 国内直连可用性 / 来源 URL / 备注。
   数字全部可溯源；无来源的数值一律留空标"待补充"，禁止编造（AGENTS.md 铁律）。
2. **不参与路由排序**：国外对比数据仅作**参考展示**，不进入 selector 的能力分 / 成本 /
   性价比计算。理由：路由决策已被 15 个国产模型的评分矩阵 + 三档策略覆盖；国外模型
   无大陆接入点 / 无 key / 时延差异是"可用性"因素而非"能力"因素，混入会污染推荐。
3. **展示层两处**：
   - CLI：`cn-llm-router compare --category <类> --complexity <低|中|高> [--strategy] [--no-availability-filter]`
     —— 输出国产推荐（复用 select）+ 国外 5 家参照表（Elo / 成本 / 可用性 / 开源）+ 结论行。
   - Web 面板：推荐结果卡下方附「国外主流模型参照」卡片（同数据）。
4. **加载**：`data_loader.load_data` 加载 `foreign_models: list[ForeignModel]`；
   文件不存在则空列表（向后兼容，不阻断启动）；不参与 `_validate` 完整性断言。

## 数据口径（as_of 2026-09-30）

- 综合成本 = 输入 × 0.6 + 输出 × 0.4（元/百万 tokens，ADR-0001 同口径；I 列活公式，代码 cost 属性同式）。
- SuperCLUE 通用榜 2026-07 排除国际模型 → 国外模型 `superclue_total` 标「未入榜（国际模型被排除）」。
- LMArena Elo：官方站未直接抓取，取 swfte 2026-09-08 快照；仅国外 Claude Fable 5.1（1525）与
  Gemini 3.1 Pro（1505）有来源数值；GPT-6 两个 / Grok 4.7 待补充（Grok 4.5=1499 备注）。
- 国内 6 模型 Elo 全部留空（对照 Sheet 口径为"待补充"，避免编造）。

## 结论行（展示固定文案，可溯源）

国产头部 Elo 1481~1500 vs 国外最高约 1525（同量级，差距 1.7%~3%）；
同档旗舰成本国产便宜 8.7~42 倍（Qwen3.8-Max 21.6 元 vs GPT-6 Astra 187.2 元；
DeepSeek-V4.1 4.4 元 vs Claude Fable 187.2 元）；国产全部官方直连、无合规风险。

> 注：国产 Elo 区间 1481~1500 与国外最高 1525 来自对比报告（基于 swfte 快照中可用国产样本），
> 报告中已标注其近似性；严格口径见打分表「国内外对照」Sheet 缺口声明。

## 影响

- `RouterData.foreign_models` 新增字段；`cli.py` 新增 compare 子命令；`web.py` 推荐结果卡新增参照区。
- 路由行为零变化（selector / gateway / classifier 未改）。
- 后续社区实测补齐国外模型真实成绩时，更新 CSV 的 `lmarena_elo` / 新增实测列即可。
