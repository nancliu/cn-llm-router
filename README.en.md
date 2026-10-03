# cn-llm-router

[中文](./README.md) | English

A router for Chinese LLMs: it classifies a task into one of 12 categories and one of 3 complexity levels, then uses a scoring matrix built from public benchmarks plus cost-performance strategies to recommend and route to the best-fit domestic model (DeepSeek / Qwen / Kimi / GLM / Doubao / Hunyuan / iFlytek Spark / MiniMax, etc.).

> Note: task categories and strategy names are Chinese strings in the API (e.g. `程序编码` = Coding, `平衡` = Balanced). Code examples below keep the literal Chinese values so they run as-is.

## Status

- ✅ Scoring table v1-20260923 (12 categories × 3 complexities × 15 models; three cost-performance strategies: Capability-first / Balanced / Cost-performance)
- ✅ Routing layer v1 (task classifier + model selector + thin OpenAI-compatible gateway), 172 tests passing (Python 3.10/3.11/3.12 via GitHub Actions CI)
- ✅ Sub-Agent multi-model orchestration (ADR-0007): split one task into subtasks, each classified and routed independently to a different model
- ✅ Tooling: Feishu score-sheet sync, online classifier eval (180 golden cases), multimodal runs, model version tracking (ADR-0008), community-score overlay (ADR-0009), CLI, PyPI packaging (v0.3.0 published), LiteLLM backend
- ✅ OpenAI-compatible serve gateway (ADR-0021): local model endpoint (default 127.0.0.1:10041, `/v1/chat/completions` + `/v1/responses`), transparent passthrough; **Codex connects with zero translation, Claude Code via litellm bridge** (real-model tool round-trips verified). See `docs/serve-guide.md`
- ✅ Online eval (2026-09-30, regression fix re-run, 180 golden cases): Qwen3.8-Max-0902 at **100% (180/180)** end-to-end is the default classifier; DeepSeek-V4.1-Flash-CED at 98.9% and ~6x faster (ADR-0020, see `reports/eval-qwen-fix-20260930.json`)

## How this differs from LiteLLM / RouteLLM

Both are called "routers", but they solve different problems: cn-llm-router is a **semantic-decision** router — *which model should handle this request*; the LiteLLM Router is a **traffic-engineering** router — *which endpoint should handle this (already chosen) model*. Its routing strategies (weighted / least-busy / TPM-RPM / latency / cost) are agnostic to request content.

| Dimension | cn-llm-router | LiteLLM Router |
|---|---|---|
| Question it answers | Which model fits this request (semantic) | Which endpoint serves this model (traffic) |
| Decision basis | Task classification (12 categories × 3 complexities) + traceable scoring matrix + 3 strategies | Load / latency / cost / quota; ignores request content |
| Output | Deterministic primary + backup + reasoning | Distribution across a deployment pool by strategy |
| Capability scoring | SuperCLUE + community evals + real VLM runs, every number with a source URL | None |
| Failover | Primary → backup, 1 retry (network / 5xx / 429) | num_retries + cooldowns + multi-level fallbacks + health checks (superset) |
| Gateway | Self-built thin OpenAI-compatible layer (ADR-0003) | LiteLLM Proxy (100+ providers) |

- **Overlap is limited to a thin layer**: OpenAI-compatible gateway and failover (LiteLLM is a superset); the core "classification × scoring matrix × strategy" does not overlap.
- **They compose**: `backend: litellm` already lets a provider connect through LiteLLM; when you need multi-endpoint load balancing / budget control / cooldowns, put LiteLLM behind the `serve` gateway — each layer does its own job.
- **Reference**: LiteLLM [Router routing strategies](https://docs.litellm.ai/docs/routing) and [Auto Routing / Adaptive Router](https://docs.litellm.ai/docs/adaptive_router) (beta; routes between cheap/expensive tiers by request type — the closest feature direction, but without a scoring matrix or explainable recommendations).

## Quickstart

```bash
pip install cn-llm-router                 # from PyPI (v0.3.0); dev install: pip install -e ".[dev]"
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
pip install cn-llm-router            # or dev install pip install -e ".[dev]" (registers the cn-llm-router command)
cn-llm-router classify "帮我写一个Python函数解析JSON"        # classify a task
cn-llm-router select --category 程序编码 --complexity 低 --no-availability-filter   # recommend a model
cn-llm-router compare --category 程序编码 --complexity 高 --no-availability-filter # recommend + foreign-model reference (Claude/GPT/Gemini/Grok)
cn-llm-router route "用SQL统计每日订单量" --no-availability-filter                 # classify + recommend + ready client
cn-llm-router serve --port 10041 --strategy 平衡 --token my-secret                 # OpenAI-compatible serve gateway (ADR-0021)
cn-llm-router list-models / list-categories / list-strategies                      # inspect data & strategies
# All commands support --json (a single JSON document on stdout, pipe/script friendly)
```

> **Harness integration (capability preserved, transparent passthrough ADR-0021)**:
> - **Codex** (zero translation): `docs/serve-guide.md` §3 — point `model_provider.base_url` at `http://127.0.0.1:10041/v1`; `model=auto` for per-request routing or `model=<logical_name>` for pinned passthrough.
> - **Claude Code** (via litellm bridge): serve also exposes `/v1/responses`; start the litellm proxy per `docs/serve-guide.md` §4 / `config/litellm-proxy.example.yaml`, real-model multi-round tool loops verified.

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
- `docs/adr/` — Architecture Decision Records (0001 scoring policy … 0021 serve gateway transparent-passthrough rule)
- `docs/spec/router-v1.md` — routing layer v1 spec
- `docs/serve-guide.md` — serve gateway integration guide (Codex zero-translation / Claude Code via litellm, ADR-0021)
- `docs/agents/` — agent working conventions (issue tracker / triage / domain)

## License

See [LICENSE](./LICENSE)
