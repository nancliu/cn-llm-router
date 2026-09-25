# ADR-0010：分类结果内存缓存

- 状态：accepted
- 日期：2026-09-25
- 关联：ADR-0002（任务分类器）、ADR-0004（数据版本 data/VERSION）

## 背景

分类器 `Classifier.classify()` 的主路径是 LLM 结构化判类（ADR-0002）：每次调用都要向分类候选链（默认 Qwen3.8-Max-0902 → DeepSeek-V4.1-Flash-CED → 豆包Seed-2.1-Pro）发一次带类别定义与复杂度锚点的长 prompt，并等待 JSON 返回。在代理批量任务、子任务编排（ADR-0007）等场景下，**相同或等价的请求会被反复判类**，重复消耗分类模型的 token 与时延，而分类结果本身在短时间内是确定的。

规则兜底与默认兜底虽然不调 LLM，但同样是 classify 的完整输出；对其缓存也能省掉重复的关键词扫描与决策路径，且不改变任何输出语义。

需要一层**内存缓存**命中即返回，把重复 classify 的边际成本降到接近零；同时必须保证：数据更新、模型链变更后绝不返回旧结果。

## 决策

在 `cn_llm_router/cache.py` 新增 `ClassifyCache`，`Classifier` 在 `classify()` 主入口处包一层"查缓存 → 未命中走原流程 → 写缓存"。

- **TTL 失效**：条目以 `time.monotonic()` 记录过期时刻，默认 3600 秒（1 小时）；过期视为未命中并惰性删除。
- **容量上限 + LRU 淘汰**：默认 1000 条；超限时淘汰最久未访问的条目（`OrderedDict.move_to_end` + `popitem(last=False)`）。
- **默认开启**：`RouterConfig.cache_enabled=True`，开箱即缓存；离线测试走规则兜底，命中与否结果一致，不破坏现有断言。
- **单进程、不加锁**：v1 假设单进程串行使用；多进程/多线程场景由调用方自行保证，不在 v1 引入锁。

## 缓存键设计

键 = `make_cache_key(prompt, classifier_models, data_version)` → SHA-256 十六进制串，三段以分隔符拼接后哈希：

1. **规范化 prompt**：`strip()` + 折叠连续空白为单空格 + 转小写（首尾/连续空白与大小写差异视为同一请求，如 `"  写  代码 "` 与 `"写 代码"` 同键；内部语义空格保留）；
2. **分类模型链**：`tuple(cfg.classifier_models)` 的 join——换候选链或换模型顺序即换键，绝不串用旧链结果；
3. **数据版本**：`data.version`（取自 `data/VERSION`，当前 `v1-20260923`）——数据/类别定义更新后旧缓存自然失配。

三段全部入键，保证"prompt 不同 / 模型链不同 / 数据版本不同"三者任意其一都会生成不同键。

## 缓存内容

缓存存**完整 Classification 对象的 dict 形式**（`dataclasses.asdict`），包含 `category / complexity / confidence / second_guess / low_confidence / fallback_reason / raw` 全部字段；命中时 `Classification(**cached_dict)` 重建，并把新增可选字段 `cached=True` 标记为本次命中。

- LLM 判类成功、规则兜底、默认兜底三类输出**统一写入缓存**——它们都是 classify 的最终输出；
- `raw` 字段可能为 `None`，重建时原样保留；非 debug 模式下 `raw` 恒为 None；
- `cached` 只是命中标记，不参与业务语义，也不写入持久化文件。

## 配置

`RouterConfig` 已新增字段（前置提交）：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `cache_enabled` | `True` | 是否启用缓存；关闭后 Classifier 不创建缓存实例，每次都走完整 classify 逻辑 |
| `cache_ttl` | `3600` | 条目存活秒数；`0` 表示写入即过期（用于测试/强制刷新） |
| `cache_max_size` | `1000` | 内存条目上限，超限 LRU 淘汰 |
| `cache_path` | `None` | 持久化 JSON 路径；`None` = 仅内存 |

均可由 `config/selector.yaml` 覆盖（加载逻辑已在 `load_config` 中）。`Classifier.__init__` 新增可选参数 `cache: ClassifyCache | None = None`：**显式注入优先**；未注入且 `cache_enabled=True` 时按上述配置自建；`cache_enabled=False` 时无缓存实例。

## 持久化接口

`cache_path` 非 None 时，`ClassifyCache` 启动 `load(path)`、进程退出由调用方显式 `save(path)`（库不注册 atexit，保持可控）。

- 文件格式：JSON，`{"version": 1, "entries": {key: value_dict}}`；
- **不落盘过期时间戳**：load 后以当前时刻重新计算 TTL（v1 简单可靠，避免跨进程时钟问题）；
- **v1 仅留接口**：以内存缓存为主，持久化是可选的重启预热能力，不做写入缓冲、不做原子替换。

## 命中率统计

`ClassifyCache.stats` 只读属性返回 `{"hits": N, "misses": N, "size": N}`：

- `get()` 命中（含未过期）→ hits+1，未命中（键不存在或已过期）→ misses+1；
- `size` 为当前内存条目数；
- 命中率 = hits / (hits + misses)，由消费方自行计算；Classifier 实例通过 `.cache.stats` 暴露。

## 不做什么

- **不缓存 select / route 结果**：select 是纯函数（无外部成本），route 依赖实时网关状态，缓存反而引入陈旧风险；
- **不做分布式缓存**（Redis 等）：v1 单进程内存足够，跨进程一致性问题不在本期；
- **不做缓存击穿/穿透防护**：热点 key 并发重建、空请求缓存等 v1 不处理；
- **不改 classify() 的公开签名与返回类型**；不改 select / route / orchestrate 行为；不改 config.py（字段已就位）。

## 影响

- 新增 `cn_llm_router/cache.py`：`ClassifyCache`（get/set/clear/stats/load/save）+ `make_cache_key`；
- `cn_llm_router/types.py`：`Classification` 新增可选字段 `cached: bool = False`；
- `cn_llm_router/classifier.py`：`classify()` 在原降级链之前查缓存、之后写缓存；`__init__` 新增 `cache` 参数并按配置自建；
- `cn_llm_router/__init__.py`：公共 `classify()` 不新增参数，由 Classifier 自建缓存；
- 新增 `tests/test_cache.py`。
