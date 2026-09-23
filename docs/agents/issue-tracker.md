# Issue tracker: GitHub

本仓库的 issue 与 spec 统一放在 GitHub Issues，用 `gh` CLI 操作。仓库由 `git remote -v` 自动推断，在 clone 内运行即可。

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`，多行正文用 heredoc。
- **读取 issue**：`gh issue view <number> --comments`，用 `jq` 过滤评论并取 labels。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，配合 `--label` / `--state` 过滤。
- **评论 issue**：`gh issue comment <number> --body "..."`
- **增删标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

## Pull requests as a triage surface

**PRs as a request surface: no.**（如后续把外部 PR 视为 feature request，改为 `yes`，`/triage` 会读取此标记）

改为 `yes` 时，PR 与 issue 共用标签和状态，用 `gh pr` 等价命令：
- **读 PR**：`gh pr view <number> --comments`、`gh pr diff <number>`
- **列出外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只保留 `authorAssociation` 为 `CONTRIBUTOR` / `FIRST_TIME_CONTRIBUTOR` / `NONE` 的（排除 OWNER/MEMBER/COLLABORATOR）
- **评论 / 打标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`

GitHub 的 issue 与 PR 共用编号空间，裸 `#42` 可能是其一：先 `gh pr view 42`，失败再 `gh issue view 42`。

## 当技能说 "publish to the issue tracker"

创建 GitHub issue。

## 当技能说 "fetch the relevant ticket"

运行 `gh issue view <number> --comments`。

## Wayfinding operations

供 `/wayfinder` 使用。**map** 是一个带 `wayfinder:map` 标签的 issue，子任务为子 issue。

- **Map**：`gh issue create --label wayfinder:map`，正文含 Notes / Decisions-so-far / Fog。
- **子任务**：以 GitHub sub-issue 关联 map（`gh api` 操作 sub-issues endpoint）；未启用 sub-issues 时，把子任务写进 map 正文的 task list，并在子 issue 顶部加 `Part of #<map>`。标签：`wayfinder:<type>`（`research` / `prototype` / `grilling` / `task`）。认领后指派给负责的开发。
- **阻塞**：优先用 GitHub 原生 issue 依赖（`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，`<blocker-db-id>` 是阻塞者的数据库 id，用 `gh api repos/<owner>/<repo>/issues/<n> --jq .id` 获取，不是 `#number` 或 `node_id`）；不可用时在子 issue 顶部写 `Blocked by: #<n>, #<n>`。
- **Frontier 查询**：列出 map 的 open 子任务（`gh issue list --state open` 按 map 的子 issue/task list 过滤），剔除有 open blocker 或已指派人的，按 map 顺序取第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`。
- **完成**：`gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，并把上下文指针（gist + 链接）追加到 map 的 Decisions-so-far。
