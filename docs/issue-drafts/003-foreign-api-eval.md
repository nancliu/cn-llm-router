---
name: 数据修正
about: 国外主流模型（Claude/GPT/Gemini/Grok）真实 API 实测与 Elo 回填（ADR-0019 开放协作）
title: "[Data] 国外主流模型真实 API 实测与 Elo 回填"
labels: ["needs-triage", "good-first-issue"]
---

> 评分/价格数字必须可溯源。无公开来源的修正不予合并；缺口应标"待补充"而非臆造（AGENTS.md 铁律）。

## 涉及模型 / 文件

- 模型：Anthropic Claude Fable 5.1、OpenAI GPT-6 Astra / GPT-6 Sol、Google Gemini 3.1 Pro、xAI Grok 4.7（`data/foreign_comparison.csv`，ADR-0019 对照数据，仅展示、不参与路由排序）
- 文件：`data/foreign_comparison.csv`
- 字段：`lmarena_elo`（当前 GPT-6 Astra / GPT-6 Sol / Grok 4.7 为"待补充"）、可选新增"实测准确率"列

## 当前值

```csv
vendor,model,region,superclue_total,lmarena_elo,price_in,price_out,...
OpenAI,GPT-6 Astra,国外,未入榜（国际模型被排除）,,72.00,360.00,...
OpenAI,GPT-6 Sol,国外,未入榜（国际模型被排除）,,14.40,72.00,...
xAI,Grok 4.7,国外,未入榜（国际模型被排除）,,14.40,43.20,...
```

已有来源值：Claude Fable 5.1 Elo=1525（swfte 2026-09-08 快照）、Gemini 3.1 Pro Elo=1505。

## 建议方案

1. **Elo 回填（无需 key）**：查 LMArena 官方或可信第三方快照，为 GPT-6 Astra / GPT-6 Sol / Grok 4.7 补 `lmarena_elo`，必须附 `source_url` 与快照日期。
2. **真实 API 实测（需国外 key）**：持有 Anthropic / OpenAI / Gemini / Grok API key 的贡献者，按 `docs/eval-setup.md` 流程注册 provider（OpenAI 兼容端点）后运行：

   ```bash
   python scripts/eval_classifier.py --models <国外模型> --output reports/eval-foreign-<ts>.json
   ```

   提交 180 题判类实测结果；可同步为 `foreign_comparison.csv` 增加"实测准确率"列并回填（带 as_of 与报告链接）。
3. 更新 `reports/国内外大模型对比_20260930.html` 对应图表与结论（如需，由维护者协助）。

## 来源 URL（必填，回填 Elo 时）

<!-- 公开榜单 / 官方文档链接；多个用换行分隔 -->

- （待贡献者提供；已知参考：LMArena https://lmarena.ai/leaderboard 、swfte 聚合 https://www.swfte.com/zh/lmarena ）

## 数据日期（as_of）

<!-- 来源页面的抓取日期，YYYY-MM-DD -->

## 补充说明

- 国外模型无大陆官方接入点、需代理；评测请求时注意网络环境。
- 国外模型"未入 SuperCLUE 榜（2026-07 榜排除国际模型）"——SuperCLUE 总分列维持"未入榜"说明，不强行换算。
- 对比数据仅展示、不参与路由排序（ADR-0019），回填不影响推荐逻辑。
