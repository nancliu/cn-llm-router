# cn-llm-router 长程任务路线图

> 创建：2026-09-24
> 基线：v0.1.0（未发布），73 pytest 全绿，数据版本 v1-20260923
> 原则：ADR 先行、数字可溯源、分阶段交付（每阶段结束有验证与交付物）、不阻塞发布

## 一、目标

在 v1 路由层（classify → select → route 单模型）基础上，完成四项核心增强与一项可选增强：

| 编号 | 优先级 | 工作项 | 核心产出 |
|---|---|---|---|
| P1-a | P1 | Sub-Agent 多模型分配编排 | `orchestrate()` API + ADR-0007 + 测试 |
| P1-b | P1 | 模型版本跟踪自动化 | `scripts/monitor_versions.py` + ADR-0008 |
| P2-a | P2 | 社区/人工评测叠加 | 第二数据源接入 + ADR-0009 + 数据层扩展 |
| P2-b | P2 | 评测扩展 | golden cases 扩充 + 多模态实测 + 评测报告 |
| P3 | 可选 | 缓存层 + 使用统计 | classify 缓存 + 成本报表 |

## 二、阶段划分

### Phase 0：规划与基线确认（本阶段）

- [x] 仓库结构探查（cn_llm_router/、data/、tests/、scripts/、docs/）
- [x] pytest 基线确认：73 passed
- [x] 本路线图编写
- 交付物：`docs/roadmap.md`

### Phase 1：P1-a Sub-Agent 多模型分配编排

**问题**：现有 `route()` 只返回单模型 client；用户需要"一个任务拆多个 sub-agent，各用不同模型"。

**工作内容**：
1. 写 ADR-0007：编排层设计（子任务描述 → 分类 → 各自 route → 返回多 client 字典）
2. 实现 `cn_llm_router/orchestrator.py`：
   - `SubTaskSpec`：`{id, description, category?, complexity?, strategy?}`（category/complexity 缺省时自动 classify）
   - `OrchestrationResult`：`{subtasks: [{spec, classification, recommendation, client}]}`
   - `orchestrate(subtasks: list[SubTaskSpec], *, strategy="平衡", ...) -> OrchestrationResult`
   - 支持按子任务类别/复杂度分别选模型；共享 config/data 加载（不重复加载）
3. 公共 API 导出（`__init__.py` 加 `orchestrate`, `SubTaskSpec`, `OrchestrationResult`）
4. CLI 扩展（可选）：`cn-llm-router orchestrate --tasks ...`
5. 测试：`tests/test_orchestrator.py`（离线 mock classify/select，验证多子任务独立路由、client 隔离、策略覆盖）

**验收标准**：
- ADR-0007 合入 `docs/adr/`
- `orchestrate()` 对 N 个子任务返回 N 个独立 RouteResult（client 不共享）
- pytest 全绿（新增 ≥6 测试）
- 文档：README 或 spec 补充编排用法示例

### Phase 2：P1-b 模型版本跟踪自动化

**问题**：现在靠人工更新打分表再 sync；厂商发版/调价无感知。

**工作内容**：
1. 写 ADR-0008：版本监控机制（抓取范围、变更检测、可溯源约束）
2. 实现 `scripts/monitor_versions.py`：
   - 读取 `data/models.csv` 的 `source_url` 与 `as_of`
   - 抓取厂商定价/模型页面（DeepSeek、阿里百炼、智谱、Moonshot、火山、腾讯、MiniMax、讯飞）
   - 对比 version / price_in / price_out / context_window 字段变化
   - 输出变更报告（JSON + 终端摘要），标注"待人工确认"项
   - 抓不到的字段标"待补充"，**禁止编造数字**
   - `--dry-run` 只校验 URL 可达性；`--update` 生成 `data/models.csv.new` 供人工 diff（不直接覆盖）
3. 测试：`tests/test_monitor_versions.py`（离线：URL 解析、diff 逻辑、报告格式；在线部分 mock）

**验收标准**：
- ADR-0008 合入
- `monitor_versions.py --dry-run` 可运行（无 key 也能跑）
- 变更检测逻辑有单测覆盖
- 所有输出数字带 source_url，缺口标"待补充"

### Phase 3：P2-a 社区/人工评测叠加

**问题**：v1 评分仅 SuperCLUE + Terminal 代理，维度单一。

**工作内容**：
1. 写 ADR-0009：第二数据源接入口径（来源、权重、与现有评分的融合方式）
2. 调研 LMArena / 其他社区评分为国产模型提供的数据覆盖情况
3. 数据层扩展：
   - `data/community_scores.csv`：`model, source, metric, value, source_url, as_of`
   - `data_loader.py` 加载社区分
   - 选择器融合：现有能力分 × α + 社区分归一化 × β（α/β 可配，默认 α=0.7 β=0.3）
4. 缺口处理：无社区分的模型不参与融合（notice 提示），**禁止编造**
5. 测试：`tests/test_community_scores.py`

**验收标准**：
- ADR-0009 合入（含来源 URL 清单与待补充项）
- 社区分数据文件存在且可加载
- 选择器融合逻辑有单测
- 无社区分的模型回退到纯 SuperCLUE 分（不报错）

### Phase 4：P2-b 评测扩展

**问题**：mm 类 CED 无 VLM 分靠文本代理；golden cases 每格仅 3 题；仅 2 家 key 参与评测。

**工作内容**：
1. golden cases 扩充：12 类 × 3 复杂度 × 4-5 题（从 108 → ~162 题）
   - 新增题目标注 `rule_testable`，保持与现有格式一致
2. 多模态实测基础设施：
   - `scripts/eval_multimodal.py`：对多模态类别题目，调用支持 VLM 的模型（GLM-5.3-Flash、Qwen3.8-Max、DeepSeek-V4.1-Flash-CED）做真实图文测试
   - 测试素材：`tests/fixtures/mm_images/`（可用公开图片 URL 或生成简单测试图）
   - 评测结果写入 `reports/`
3. 补齐 key 的模型纳入 `eval_classifier.py` 评测（当前已配火山+阿里，确保所有 key 已配模型都跑）
4. 运行全量评测，产出 `reports/eval-20260924.json` + 摘要

**验收标准**：
- golden cases ≥ 144 题（每格 ≥4）
- 多模态评测脚本可运行（有 key 时）
- 评测报告产出，含各模型端到端准确率与复杂度命中率
- pytest 全绿

### Phase 5（可选）：P3 缓存层与使用统计

**前置条件**：P1-P2 完成且时间允许。

**工作内容**：
1. 分类缓存：`classify()` 结果按 prompt 哈希缓存（TTL 可配，默认 1h），省分类 token
2. 使用统计：`route()` / `orchestrate()` 记录每次调用的模型、token 用量、估算成本，输出 JSONL
3. 成本报表：`scripts/cost_report.py` 汇总月度/按模型成本

**验收标准**：缓存命中有单测；统计输出格式稳定。

## 三、约束与风险

| 约束 | 说明 |
|---|---|
| ADR 先行 | 影响架构/口径的改动先写 ADR 再实现（Phase 1/2/3 均需） |
| 数字可溯源 | 所有评分/价格数字必须有 source_url；抓不到标"待补充"，禁止编造 |
| 测试门禁 | 每阶段结束 pytest 全绿；改动打分表数据需过 excel_formula_verify / lark_sheet_selfcheck |
| 文档简体中文 | ADR、spec、README 均用简体中文；代码标识符英文 |
| 不破坏现有 API | `classify` / `select` / `route` 签名不变，新功能以新增 API 方式提供 |

**风险**：
- 厂商页面结构变化导致 monitor 抓取失败 → 设计为容错，失败标"待补充"不报错
- LMArena 国产模型覆盖不足 → ADR 中明确覆盖范围，缺口标待补充
- 多模态实测需要图片素材与 key → 脚本支持 dry-run，有 key 时才真实调用

## 四、v0.1.0 发布步骤（用户侧操作，不阻塞开发）

发布是用户侧操作，与开发并行：

1. 用户在 GitHub 仓库 Settings → Environments 创建 `pypi` 环境（trusted publishing）
2. 在 PyPI 项目中配置 trusted publisher（仓库 nancliu/cn-llm-router，workflow `release.yml`，environment `pypi`）
3. 本地打 tag：`git tag v0.1.0 && git push origin v0.1.0`
4. GitHub Actions `release.yml` 自动构建并发布到 PyPI

> 此项不阻塞 Phase 1-5 的开发；各阶段功能可在 v0.1.0 后以 v0.2.0+ 发布。

## 五、阶段状态

- [x] Phase 0：规划与基线确认（73 测试基线，roadmap.md）
- [x] Phase 1：P1-a 编排层（ADR-0007 + orchestrator.py + 9 测试，82 测试全绿）
- [x] Phase 2：P1-b 版本监控（ADR-0008 + monitor_versions.py + 11 测试，93 测试全绿）
- [x] Phase 3：P2-a 社区评测（ADR-0009 + community_scores.csv 9 条 LMArena 数据 + 选择器融合 + 8 测试，101 测试全绿）
- [x] Phase 4：P2-b 评测扩展（golden cases 108→180 + eval_multimodal.py + 三模型全量评测报告，101 测试全绿）
- [ ] Phase 5：P3 缓存与统计（可选，待用户确认是否继续）
