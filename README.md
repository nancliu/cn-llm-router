# cn-llm-router

[English](./README.en.md) | 中文

国内大模型选择器：识别任务类别（12 类）与复杂度（3 级），依据公开评测合成的评分矩阵与性价比策略，推荐并路由到最合适的国产大模型（DeepSeek / Qwen / Kimi / GLM / 豆包 / 混元 / 讯飞 / MiniMax 等）。

## 状态

- ✅ 打分表 v1-20260923（12 任务类别 × 3 复杂度 × 15 模型，含三档性价比策略：纯能力优先 / 平衡 / 性价比优先）
- ✅ 路由层 v1（任务分类器 + 模型选择器 + OpenAI 兼容薄网关），172 个测试通过（Python 3.10/3.11/3.12，GitHub Actions CI）
- ✅ Sub-Agent 多模型编排（ADR-0007）：一个任务拆多个子任务，每个独立判类选模型分配不同大模型
- ✅ 工具链：飞书打分表同步、分类在线评测（180 golden cases）、多模态实测、模型版本跟踪（ADR-0008）、社区评测叠加（ADR-0009）、CLI、PyPI 打包（wheel 已验证）、LiteLLM backend
- ✅ OpenAI 兼容 serve 网关（ADR-0021）：本地模型端点（默认 127.0.0.1:10041，含 /v1/chat/completions + /v1/responses），透明转发不衰减；**Codex 零转换层直连、Claude Code 经 litellm 桥接**（真实模型 tool 往返已实测），接入见 `docs/serve-guide.md`
- ✅ 在线评测（2026-09-30 分类规则回归修复后全量重跑，180 golden cases）：Qwen3.8-Max-0902 端到端 **100%（180/180）** 为默认分类模型；DeepSeek-V4.1-Flash-CED 98.9% 且快约 6 倍（ADR-0020，报告见 `reports/eval-qwen-fix-20260930.json`）

## 与 LiteLLM / RouteLLM 的定位区别

都叫 "Router"，但解决的不是同一个问题：cn-llm-router 是**语义决策**型路由器——这条请求**该用哪个模型**；LiteLLM Router 是**流量工程**型路由器——同一模型**走哪个端点**，其路由策略（加权 / 最少繁忙 / 按 TPM-RPM / 按时延 / 按成本）与请求内容无关。

| 维度 | cn-llm-router | LiteLLM Router |
|---|---|---|
| 决策问题 | 这条请求该用哪个模型（语义决策） | 同一模型走哪个端点（流量工程） |
| 决策依据 | 任务分类（12 类 × 3 复杂度）+ 可溯源评分矩阵 + 三档策略 | 负载 / 时延 / 成本 / 配额，不看请求内容 |
| 输出 | 确定性主选 + 备选 + 推荐理由 | 按策略分发到 deployment 池 |
| 能力评分 | SuperCLUE + 社区评测 + VLM 实测，全部带来源 URL | 无 |
| 失败切换 | 主 → 备 1 次（网络 / 5xx / 429） | num_retries + cooldown + 多级 fallback + 健康检查（超集） |
| 网关 | 自建 OpenAI 兼容薄层（ADR-0003） | LiteLLM Proxy（100+ provider） |

- **重叠仅限薄层**：OpenAI 兼容网关与失败切换（LiteLLM 为超集）；核心"分类 × 评分矩阵 × 策略"不重叠。
- **互补使用**：`backend: litellm` 已支持经 LiteLLM 直连上游；需要多端点负载均衡 / 预算管控 / 冷却时，可把 LiteLLM 叠在 serve 网关之后，各管一层。
- **参考**：LiteLLM [Router 路由策略](https://docs.litellm.ai/docs/routing) 与 [Auto Routing / Adaptive Router](https://docs.litellm.ai/docs/adaptive_router)（beta，按请求类型在贵/便宜档间路由，是方向最接近的功能，但无评分矩阵与可解释推荐）。

## 与国外主流模型对比

为使用国产模型提供信心依据：国产头部（Qwen / GLM / DeepSeek / Kimi / 豆包 / 混元）与国外主流旗舰（Claude / GPT / Gemini / Grok）公开榜单 + 官方定价同口径对比（数据 as_of 2026-09-30，全部数字带来源 URL，详见打分表「国内外对照」Sheet 与对比报告）：

| 维度 | 结论 |
|---|---|
| **能力** | 同量级：国产头部 Elo 1481~1500 vs 国外最高约 1525，差距 1.7%~3% |
| **成本** | 便宜 **8.7~42 倍**：同档旗舰 Qwen3.8-Max 21.6 元 vs GPT-6 Astra 187.2 元（8.7×）；DeepSeek-V4.1 4.4 元 vs Claude Fable 187.2 元（42×） |
| **可用性** | 国产全部官方直连、无合规风险；国外均无大陆官方接入点，需代理 |
| **结论** | 通用文本 / 编码 / 数据分析等主流场景可放心使用国产头部；高复杂度前沿任务建议国产头部 + 社区实测验证 |

- 飞书打分表「国内外对照」Sheet：https://feishu.doubao.com/sheets/Ku08sUbNHhtNggtaa82c99qYnwe
- 对比报告（交互图表）：`reports/国内外大模型对比_20260930.html`

> 口径说明：国外旗舰未入 SuperCLUE 榜，能力分用 LMArena Elo 近似；综合成本 = 输入 × 0.6 + 输出 × 0.4（元/百万 tokens）；真实 API 实测留给开源社区。

对比数据已作为**参考数据资产**融入 router（`data/foreign_comparison.csv`，仅展示、不参与路由排序）：
CLI `cn-llm-router compare` 与 Web 面板推荐结果卡均附「国外主流模型参照」（ADR-0019）。

## 快速使用

```bash
pip install cn-llm-router                 # PyPI 安装（v0.3.0）
# 开发安装：pip install -e ".[dev]"
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

## Sub-Agent 多模型编排

一个大任务拆成多个性质不同的子任务（编码 / 文书 / 数据分析……），每个子任务独立判类、选模型、拿独立 client（ADR-0007）：

```python
from cn_llm_router import SubTaskSpec, orchestrate

result = orchestrate([
    SubTaskSpec(id="coding", description="实现一个带重试的 HTTP 客户端", strategy="纯能力优先"),
    SubTaskSpec(id="doc",    description="把上面的改动整理成技术周报", strategy="性价比优先"),
    SubTaskSpec(id="data",   description="统计最近 30 天失败率并分组出趋势"),
], strategy="平衡", availability_filter=True)

for r in result.subtasks:
    print(r.spec.id, r.classification.category, "->", r.recommendation.primary.logical_name)
    # resp = r.client.chat.completions.create(
    #     model=r.recommendation.primary.logical_name,
    #     messages=[{"role": "user", "content": r.spec.description}])
```

零 key 离线示例：`python examples/orchestrate_demo.py`（规则兜底分类、不发请求）。接入 Cursor / Claude Code / 豆包等 Agent 客户端的完整集成方式见 [docs/orchestrator-guide.md](docs/orchestrator-guide.md)。

## CLI

```bash
pip install cn-llm-router            # 或开发安装 pip install -e ".[dev]"（注册 cn-llm-router 命令）
cn-llm-router classify "帮我写一个Python函数解析JSON"        # 任务分类
cn-llm-router select --category 程序编码 --complexity 低 --no-availability-filter   # 模型推荐
cn-llm-router compare --category 程序编码 --complexity 高 --no-availability-filter # 推荐 + 国外主流模型参照（Claude/GPT/Gemini/Grok）
cn-llm-router route "用SQL统计每日订单量" --no-availability-filter                 # 分类+推荐+就绪客户端
cn-llm-router serve --port 10041 --strategy 平衡 --token my-secret                 # OpenAI 兼容 serve 网关（ADR-0021；默认单实例守护：端口已有本应用实例时自动关旧启新，--no-restart 关闭）
cn-llm-router list-models / list-categories / list-strategies                      # 数据与策略查看
# 全部命令支持 --json（stdout 仅一份 JSON，可管道/脚本化）
```

> **harness 接入（能力不变弱，透明转发铁律 ADR-0021）**：
> - **Codex**（零转换层直连）：`docs/serve-guide.md` 第 3 节 `config.toml` 示例——`model_provider.base_url` 指向 `http://127.0.0.1:10041/v1`，`model=auto` 判类路由或 `model=<logical_name>` 点名透传。
> - **Claude Code**（经 litellm 桥接）：serve 同时提供 `/v1/responses`，`docs/serve-guide.md` 第 4 节 + `config/litellm-proxy.example.yaml` 一条命令启动 litellm proxy 即可，真实模型多轮 tool 往返已实测。

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
- `docs/adr/` — 架构决策记录（0001 评分口径 … 0021 serve 网关透明转发铁律）
- `docs/spec/router-v1.md` — 路由层 v1 规格
- `docs/serve-guide.md` — OpenAI 兼容 serve 网关接入指南（Codex 零转换直连 / Claude Code 经 litellm 桥接，ADR-0021）
- `docs/orchestrator-guide.md` — Sub-Agent 编排实战指南（Cursor / Claude Code / 豆包集成）
- `docs/eval-setup.md` — 评测 key 清单与全量评测配置
- `docs/contributing.md` — 参与开发（环境搭建 / ADR 流程 / 数据贡献规范）
- `docs/agents/` — agent 工作约定（issue tracker / triage / domain）

## License

见 [LICENSE](./LICENSE)
