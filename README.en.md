# cn-llm-router

[中文](./README.md) | English

A router for Chinese LLMs: it classifies a task into one of 12 categories and one of 3 complexity levels, then uses a scoring matrix built from public benchmarks plus cost-performance strategies to recommend and route to the best-fit domestic model (DeepSeek / Qwen / Kimi / GLM / Doubao / Hunyuan / iFlytek Spark / MiniMax, etc.).

> Note: task categories and strategy names are Chinese strings in the API (e.g. `程序编码` = Coding, `平衡` = Balanced). Code examples below keep the literal Chinese values so they run as-is.

## Status

- ✅ Scoring table v1-20260923 (12 categories × 3 complexities × 15 models; three cost-performance strategies: Capability-first / Balanced / Cost-performance)
- ✅ Routing layer v1 (task classifier + model selector + thin OpenAI-compatible gateway), 100+ tests passing (Python 3.10/3.11/3.12 via GitHub Actions CI)
- ✅ Sub-Agent multi-model orchestration (ADR-0007): split one task into subtasks, each classified and routed independently to a different model
- ✅ Tooling: Feishu score-sheet sync, online classifier eval (180 golden cases), multimodal runs, model version tracking (ADR-0008), community-score overlay (ADR-0009), CLI, PyPI packaging (wheel verified), LiteLLM backend
- ✅ Online eval (2026-09-24, Volcengine coding-plan / Bailian token-plan, 180 golden cases): Qwen3.8-Max-0902 at 99.4% end-to-end is the default classifier; DeepSeek-V4.1-Flash-CED at 98.9% and ~6x faster (see `reports/eval-20260924.json`)

## Quickstart

```bash
pip install -e ".[dev]"
# Works offline (no key needed): classify + recommend (availability_filter=False compares the full set)
python - <<'PY'
from cn_llm_router import classify, select, route

# 1) Classify (LLM-based; falls back to rules/defaults without a key, flagged low_confidence)
print(classify("帮我写一个Python函数解析JSON"))

# 2) Recommend (deterministic; data/*.csv is the single source of truth)
rec = select("程序编码", "低", strategy="平衡", availability_filter=False)
print(rec.primary.logical_name, rec.backup.logical_name, rec.notice)

# 3) Route: get an OpenAI-compatible client (auto-failover on failure) — needs an API key
cp .env.example .env   # fill in vendor keys (e.g. ZHIPU_API_KEY)
result = route("写一个Python函数解析JSON", strategy="平衡")
completion = result.client.chat.completions.create(
    model=result.recommendation.primary.logical_name,
    messages=[{"role": "user", "content": "…"}],
)
PY
```

Three strategies: `纯能力优先` (Capability-first) / `平衡` (Balanced, default) / `性价比优先` (Cost-performance). By default recommendations are filtered to models whose keys are configured; set `availability_filter: false` in `config/selector.yaml` to compare the full set.

## Sub-Agent Multi-Model Orchestration

Split a big task into heterogeneous subtasks (coding / writing / data analysis…); each subtask is classified, scored, and given its own independent client (ADR-0007):

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

Zero-key offline demo: `python examples/orchestrate_demo.py` (rule-based classification, no network calls). For integrating with Cursor / Claude Code / Doubao and other Agent clients, see [docs/orchestrator-guide.md](docs/orchestrator-guide.md) (Chinese).

## CLI

```bash
pip install -e ".[dev]"   # registers the cn-llm-router command; or python -m cn_llm_router
cn-llm-router classify "帮我写一个Python函数解析JSON"        # classify a task
cn-llm-router select --category 程序编码 --complexity 低 --no-availability-filter   # recommend a model
cn-llm-router route "用SQL统计每日订单量" --no-availability-filter                 # classify + recommend + ready client
cn-llm-router list-models / list-categories / list-strategies                      # inspect data & strategies
# All commands support --json (a single JSON document on stdout, pipe/script friendly)
```

## Tooling (scripts/)

| Script | Purpose |
|---|---|
| `scripts/sync_from_lark.py --url <sheet-url>` | Sync Feishu score sheet → `data/*.csv` (idempotent; needs `lark-cli`; URL can also live in `CN_LLM_ROUTER_SHEET_URL`) |
| `scripts/eval_classifier.py [--models A,B] [--dry-run] [--list-ready]` | Online classifier eval: 180 golden cases (12 categories × 3 complexities × 5), reports end-to-end / direct-LLM accuracy, per-category matrix, confusion matrix, and recommends a default classifier. Needs at least one classifier model key. `--list-ready` prints which models have keys configured without calling any LLM |
| `scripts/export_data.py <snapshot.json>` | One-off export (reused internally by the sync script) |

## Gateway backend

Each provider in `config/providers.yaml` has an optional `backend` (default `openai`):

- `openai`: direct OpenAI-compatible endpoint (base_url + api_key)
- `litellm`: via LiteLLM (`provider/api_model` routing, unified API); needs `pip install "cn-llm-router[litellm]"`

Failover to backup is automatic for network / 5xx / 429 errors only; auth errors (e.g. 401) raise immediately. Tune or disable via `max_failover` in `config/selector.yaml`.

## Contributing

See [docs/contributing.md](docs/contributing.md) (Chinese): dev setup, ADR workflow, conventions, and data-contribution rules (every score/price must carry a `source_url` and `as_of`; no fabricated numbers).

## Docs

- `CONTEXT.md` — project context & glossary
- `docs/adr/` — Architecture Decision Records (0001 scoring policy / 0002 classifier / 0003 gateway / 0004 data source / 0005 availability filter)
- `docs/spec/router-v1.md` — routing layer v1 spec
- `docs/agents/` — agent working conventions (issue tracker / triage / domain)

## License

See [LICENSE](./LICENSE)
