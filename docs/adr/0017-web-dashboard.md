# ADR-0017：本地面板（Web Dashboard）

- 状态：accepted
- 日期：2026-09-27
- 关联：ADR-0010（分类缓存）、ADR-0011（使用统计与成本报表）、ADR-0012（CLI 增强）

## 背景

ADR-0012 已把缓存状态与成本报表接入 `cn-llm-router` CLI：`cache-status` 打印缓存配置，`cost-report` 复用 `scripts/cost_report.py` 输出月度账单。但命令行对"看一眼、试一把"的场景仍不够直观：

- 想试一次推荐时要手敲 `cn-llm-router route "<prompt>" --strategy ...`，表单化交互成本高；
- 成本报表是纯文本表格，按月切换、按模型/类别分组的直观对比不如网页；
- 缓存命中率（hits/misses/size）在 CLI 短进程里无意义（ADR-0012 已明确不纳入），但在一个长驻本地面板进程里，复用同一个 `Classifier` 实例就能真实累积命中数，值得可视化。

需要一个零依赖、本地即用的小面板，把"推荐查询 / 成本报表 / 缓存状态"三件事放到浏览器里。

## 决策

**用 Python 标准库 `http.server`（`BaseHTTPRequestHandler` + `HTTPServer`）实现本地面板，零外部依赖，三个页面：**

1. `GET /` —— 首页（推荐查询）：HTML 表单输入 prompt、选择策略（纯能力优先/平衡/性价比优先）、`availability_filter` 开关；提交后渲染分类结果（类别/复杂度/置信度）与推荐（主选/备选模型、能力分、成本、理由）；页面底部列出 12 类定义与 15 模型注册表（从 `data/` 加载）。
2. `POST /route` —— 推荐查询处理：接收 form data，用面板进程内**持久的 `Classifier` 实例**做分类（缓存可累积命中），再调用 `selector.select` 选模型；`availability_filter=True` 且无可用 key 时给出友好提示，建议关闭过滤看全量推荐。
3. `GET /cost` —— 成本报表：读取 `cfg.stats_log_path` 的 JSONL，**复用 `scripts/cost_report.py` 的 `load_events` / `filter_month` / `summarize`**，不重复实现汇总；支持 `?month=YYYY-MM`；无日志时提示"暂无统计数据"。
4. `GET /cache` —— 缓存状态：展示缓存配置（enabled/ttl/max_size/path）与实例统计（hits/misses/size），并提示"长驻进程复用 Classifier 实例可获缓存命中收益"。

### 关键取舍

- **零外部依赖**：只标准库（`http.server` / `html` / `json` / `urllib.parse` / `threading` 不需要），不引入 Flask/FastAPI，不引入前端框架，纯 HTML + 内联 CSS。
- **复用现有 API，不重复实现**：分类用 `Classifier.classify`，选模型用 `selector.select`，成本汇总用 `cost_report.summarize`，缓存用 `ClassifyCache.stats`；不改 `classify` / `select` / `route` 的签名与行为。
- **持久 Classifier 实例换缓存命中**：公共 `route()` 每次新建 `Classifier`，缓存按实例生命周期清零；面板在 `run_server` 里建一个长驻 `Classifier`，`POST /route` 复用它分类，`/cache` 读它的 `cache.stats`，命中数才能跨请求累积。
- **单线程 `HTTPServer`**：遵循 `ClassifyCache` "单进程串行使用、不加锁"的约定，本地单人面板无需并发；请求串行处理，缓存读写无竞态。

## 不做什么

- 不做用户认证 / 登录（仅本机访问）；
- 不做生产部署 / 反向代理 / 进程管理（不考虑 gunicorn、systemd）；
- 不做实时 WebSocket / SSE 推送（页面是请求-响应式刷新）；
- 不引入前端框架（React/Vue 等），不写打包链路；
- 不修改 `classify` / `select` / `route` 的签名和行为；面板不触发网关真实调用（只到推荐为止，不建 completion client）。

## 安全

- 仅监听 `127.0.0.1`（默认端口 8765），不绑定 `0.0.0.0`，不暴露公网；
- 所有用户输入（prompt、查询参数）经 `html.escape` 转义后再渲染，防 XSS；
- 不读取/外传任何 API key，页面只展示推荐结果与本地统计。

## 影响

- 新增 `cn_llm_router/web.py`：`run_server(host, port, config)` 入口与页面渲染。
- 新增 `scripts/start_web.py`：命令行启动脚本。
- `cn_llm_router/cli.py`：新增 `web` 子命令（`cn-llm-router web [--port 8765]`）。
- 新增 `tests/test_web.py`：对随机端口起服务、发请求验证四个页面。
- 无新依赖；无配置格式变更。
