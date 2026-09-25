# ADR-0012：CLI 增强（缓存状态 + 统计/成本报表入口）

- 状态：accepted
- 日期：2026-09-25
- 关联：ADR-0010（分类缓存）、ADR-0011（使用统计与成本报表）

## 背景

P3-1（ADR-0010）已落地分类缓存，P3-2（ADR-0011）已落地使用统计 JSONL 与 `scripts/cost_report.py` 月度账单脚本。但二者对命令行用户仍是"黑盒"：

- 不知道当前缓存开关、TTL、容量上限、持久化路径是否生效；
- 不知道某次 `classify` / `route` 是否真的写了统计日志，也不知道日志落在哪；
- 月度账单只能手动 `python scripts/cost_report.py`，与 `cn-llm-router` CLI 入口割裂；
- 统计默认关闭（`stats_enabled=False`），用户想临时记一次用量时必须改配置文件，成本高。

需要在不引入新依赖、不改公共 API 签名的前提下，把缓存状态与成本报表接到现有 CLI。

## 决策

**增强现有 `cn-llm-router` CLI，新增两个子命令，并给 `classify` / `route` 加 opt-in 的 `--stats` 开关：**

1. `cn-llm-router cache-status`：打印当前缓存配置（`cache_enabled` / `cache_ttl` / `cache_max_size` / `cache_path`）与实例生命周期提示；支持 `--json`。
2. `cn-llm-router cost-report [--log PATH] [--month YYYY-MM] [--json]`：复用 `scripts/cost_report.py` 的 `load_events` / `filter_month` / `summarize` / `print_report`，不重复实现汇总逻辑；`--log` 缺省取 `cfg.stats_log_path`，`--month` 缺省当月。
3. `classify` / `route` 新增 `--stats` flag：传入时用 `dataclasses.replace(cfg, stats_enabled=True)` 临时开启本次统计，调用结束后在 stderr 提示日志路径；不传时行为与现状完全一致。

### 关键取舍

- **复用公共 API 而非旁路记录**：`cmd_classify` / `cmd_route` 改为调用 `cn_llm_router.classify()` / `route()`（二者已在 `stats_enabled=True` 时写 JSONL 事件），避免在 CLI 里重复构造事件字段。
- **统计提示走 stderr**：`--json` 模式下 stdout 必须是纯一份 JSON，统计提示与"未找到日志"等诊断信息一律走 stderr，不污染 stdout。
- **cost-report 跨目录导入**：`scripts/` 不在包路径，CLI 内用 `sys.path.insert` 把仓库 `scripts/` 加入后 `import cost_report`，与 `tests/test_stats.py` 现有做法一致。

## 不做什么

- 不做 Web 面板 / TUI 交互界面（留作后续可选增强，本次零新依赖）；
- 不修改 `cache.py` / `stats.py` / `config.py`，全部复用已有 API；
- 不修改现有子命令（classify/select/route/list-*）的默认行为；`--stats` 是 opt-in，不传时统计仍默认关闭；
- 不把缓存命中率（hits/misses）纳入 `cache-status`：CLI 每次调用是全新进程、全新 `Classifier` 实例，命中数在进程内即清零，跨进程无意义；只展示配置侧参数。

## 影响

- `cn_llm_router/cli.py`：新增 `cmd_cache_status` / `cmd_cost_report`，`cmd_classify` / `cmd_route` 改为委托公共 `classify()` / `route()` 并接 `--stats`；subparsers 注册新命令。
- 新增 `tests/test_cli_enhancement.py`：覆盖 cache-status 字段、cost-report 空日志、`route --stats` 写日志、cost-report --json 结构。
- 无新依赖；无配置格式变更。
