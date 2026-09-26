# ADR-0014：分类缓存持久化落地

- 状态：accepted
- 日期：2026-09-27
- 关联：ADR-0010（分类结果内存缓存）、ADR-0004（数据版本）

## 背景

ADR-0010 留下了 `cache_path` 配置与 `load()/save()` 接口，但明确"v1 仅留接口"：`save()` 不落时间戳，`load()` 后给所有条目重新分配完整 TTL。这在重启预热场景有两个问题：

1. **无法识别真过期条目**：上次进程刚写、TTL 还剩 5 分钟的条目，重启后又被白白授予完整 1 小时 TTL，TTL 形同虚设；
2. **单调时钟不可跨进程**：内存侧用 `time.monotonic()` 记过期时刻，它在进程重启后归零，不能直接写盘。

需要把持久化补全为可用能力：进程重启后热启动、已过期条目不复活、不破坏纯内存默认行为。

## 决策

在 `cn_llm_router/cache.py` 内补全持久化：

- **盘上时间用 wall clock**：每条目落盘时记录 `created_at = time.time()`（unix 秒），不再用单调时钟；
- **加载时按剩余 TTL 复活**：`load()` 逐条检查 `age = time.time() - created_at`：
  - `ttl <= 0` 或 `age >= ttl` → 已过期，**不加载**；
  - 否则在本进程内以 `time.monotonic() + (ttl - age)` 重建过期时刻——条目只活"剩余寿命"，TTL 语义跨进程保持；
- **文件格式**：`{"version": 1, "entries": {"<key>": {"value": {...}, "created_at": <unix秒>}}}`；
- **向后兼容**：`load()` 遇到旧格式（entry 直接是 value dict、无 `created_at`）时按"刚创建"处理（授予完整 TTL），不报错；
- **构造即加载**：`ClassifyCache(path=...)` 非 None 时 `__init__` 自动 `load()`；文件不存在视为空缓存；
- **`flush()`**：主动写盘；默认**不**在每次 set 时落盘（避免高频 classify 的 IO 放大）；
- **`auto_save`**：构造参数，`True` 时每次 `set()` 后同步 `save()`（v1 简单实现，不做异步/防抖）。`RouterConfig.cache_path` 非 None 时由 `Classifier` 自建缓存并传 `auto_save=True`。

## 权衡

- **wall clock 可被用户回拨**：`time.time()` 回拨会让条目"变年轻"。v1 接受此风险——分类缓存只是省钱预热，最坏后果是某条目多活一小会儿，不影响正确性（键仍绑定模型链与数据版本，ADR-0010）。
- **不做原子写/写缓冲**：直接 `write_text`，进程崩在写盘中间可能留下半截 JSON；`load()` 解析失败时……v1 选择让异常上抛（半截文件是明显事故，比静默吞掉安全）；调用方按需重试。
- **auto_save 同步写盘**：每次 set 一次小 JSON 写，条目量大时是开销；但 v1 默认 `max_size=1000`、单文件 < 数 MB，可接受；后续如需优化再加防抖/后台线程。
- **不改默认行为**：`cache_path=None`（默认）时纯内存、零磁盘 IO，与 ADR-0010 完全一致。

## 影响

- `cn_llm_router/cache.py`：内部条目结构由 `(expires_mono, value)` 扩展为 `(expires_mono, created_wall, value)`；`save()/load()` 按新格式读写；新增 `auto_save` 构造参数与 `flush()` 方法。
- `cn_llm_router/classifier.py`：自建缓存时 `path=cfg.cache_path, auto_save=bool(cfg.cache_path)`；`config.py` 无需改动（字段已就位）。
- 新增 `tests/test_cache_persistence.py`：往返命中、过期剔除、文件格式、无 path 不碰盘、auto_save 即时写盘、旧格式兼容。
