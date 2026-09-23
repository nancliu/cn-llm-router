# AGENTS.md — 仓库代理约定

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues（使用 `gh` CLI 操作）。见 `docs/agents/issue-tracker.md`。

### Triage labels

五个规范 triage 角色映射到 GitHub labels：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`。见 `docs/agents/triage-labels.md`。

### Domain docs

Single-context：仓库根 `CONTEXT.md` + `docs/adr/`。见 `docs/agents/domain.md`。

## 项目约定

- 文档默认简体中文；代码标识符用英文。
- 任何评分/价格数字必须可溯源（来源 URL 写入 ADR 或评分矩阵口径区）；禁止编造评测数字，缺口标注"待补充"。
- 影响架构/口径的改动先写 ADR 或开 issue，再实现。
- 路由层技术栈：Python（具体结构在路由层设计时确定）。
