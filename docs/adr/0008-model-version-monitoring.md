# ADR-0008：模型版本跟踪自动化（厂商页面抓取 + 人工确认）

- 状态：accepted
- 日期：2026-09-24
- 关联：ADR-0004（data/*.csv 为唯一真相源）

## 背景

`data/models.csv`（15 个模型）的 `version` / `price_in` / `price_out` / `context_window` 等字段当前完全靠人工更新：厂商发版或调价后，维护者需要手动翻厂商定价页、改打分表、再 sync 进仓库。这导致两类问题：

1. **滞后**：厂商已经发新版/调价，仓库里还停留在上一个 `as_of`，路由层 cost 计算基于过期价格；
2. **漏报**：小厂商页面改动无人盯，等到用的时候才发现价格口径对不上。

需要一个自动化手段定期"看一眼"厂商页面，把变更暴露出来，但**不**让脚本自动改注册表。

## 决策

实现 `scripts/monitor_versions.py`：

- 读取 `data/models.csv`，对每个模型取 `source_url` 列的第一个 URL；
- 用标准库 `urllib.request` 抓取页面（不引入 requests/httpx 等新依赖），设置 `User-Agent`，`timeout=15s`；
- 用正则 + 关键词启发式（外加针对 DeepSeek / 阿里百炼 / 智谱 BigModel / Moonshot Kimi 的特定 pattern）从页面里尝试提取 `version` / `price_in` / `price_out` / `context_window`；
- 与 CSV 现有值对比，输出摘要与 JSON 报告，**供人工确认后再入库**。

CLI：

```
python scripts/monitor_versions.py [--dry-run] [--update] [--output reports/version-monitor-<ts>.json]
```

- `--dry-run`：只校验 URL 可达性（状态码），不做内容解析；
- `--update`：把检测到的变更写入 `data/models.csv.new`（不覆盖原文件），`as_of` 更新为当天，`source_url` 保留；
- 缺省 `--output` 落到 `reports/version-monitor-<timestamp>.json`。

## 监控范围

只盯四个字段（其余字段变化频率低，v1 不监控）：

| 字段 | 说明 |
| --- | --- |
| `version` | 模型版本号/发布日期 |
| `price_in` | 输入价格（元/百万 tokens） |
| `price_out` | 输出价格（元/百万 tokens） |
| `context_window` | 上下文窗口 |

`open_source` / `license` / `architecture` / `platform` 等字段变化频率低、且需要人工判断语义，v1 不自动抓取。

## 可溯源约束（硬约束）

- 所有抓取到的数字/版本号必须记录其 `source_url`（即本次抓取用的那个 URL），写进 JSON 报告；
- 抓不到的字段一律标 **"待补充"**，**禁止猜测、禁止编造**；
- 抓取到的价格若单位不是"元/百万 tokens"（例如美元、元/千 tokens、元/张），new_value 里标注单位并标记"待人工换算"，不直接塞进 CSV；
- `--update` 生成的 `.new` 文件里，只有"明确抓到且单位口径一致"的字段才替换；其余字段保留 CSV 原值。

## 安全边界

- **不直接覆盖 `data/models.csv`**：`--update` 只生成 `data/models.csv.new`，由人工 `diff` 确认后手动替换；
- 脚本对 `data/models.csv` 只读；
- 不写飞书、不调厂商 API、不做任何写操作（除报告文件与 `.new` 文件）。

## 容错

- 单个模型抓取失败（网络异常、超时、HTTP 非 2xx、页面结构变化导致正则全miss）→ 该模型 status 标 **"抓取失败"**，**不抛异常、不中断整体流程**；
- 全部失败 / 无网络：脚本正常退出，JSON 报告里每条都是"抓取失败"，不报错码；
- 页面解析出多个候选值时，取第一个候选并在报告里留痕（v1 不做歧义仲裁）；
- `--dry-run` 模式下即使全部 URL 不可达也只报状态码，不解析、不 diff。

## 影响

- 新增 `scripts/monitor_versions.py` 与 `tests/test_monitor_versions.py`；
- 新增报告目录产物 `reports/version-monitor-*.json`（可进 .gitignore 或定期清理，v1 直接保留）；
- 人工流程：定期跑脚本 → 看 JSON 报告 → 对有变更的模型人工复核 → `diff data/models.csv data/models.csv.new` → 确认无误后替换 CSV、跑 pytest。
