# YaFip 架构（当前代码）

> AI agent 协作规则见 `AGENTS.md`；红线见 `INVARIANTS.md`。

## 1. 分层

```
web/ (UMD 前端) ──fetch──> api/ (FastAPI 路由)
                                │ get_ctx → AppContext (context.py)
                                ▼
                        data/ (业务层)
           service(提交校验) · fetcher(抓取编排) · whitelist · quality · staging
                                │                    │
                        executors.py(注册表)   repository.py(读写)
                                ▼                    ▼
                        data/tasks/(handler)   core/db.py(SQLite WAL)
```

- **api/**：只做参数校验与响应包装（`ok/page/ApiError`），不含业务；跨站写请求有 Origin 校验。
- **core/**：`db.py` 线程本地连接 + `tx()`（BEGIN IMMEDIATE）；`task_center.py` 通用任务状态机 + 限流；`audit/sse/lifecycle`。
- **data/**：业务与数据源；`repository.py` 是唯一 DB 读写实现（行情写只被 staging 调用）。
- **data/tasks/**：任务 handler（元数据/白名单/日K），按数据源绑定独立 Fetcher。

## 2. 数据模型（13 张业务表，`core/schema.sql`，user_version=10）

| 表 | 主键 | 职责 |
|---|---|---|
| `instrument` | (market, symbol) | 全量元数据（ext_json 存异构字段） |
| `instrument_metric` | (market, symbol, metric_key) | 异构指标（当前市值等，不打平） |
| `bar_daily` | (market, symbol, trade_date) | 原始日 K（source/currency/vol_unit/amount_unit 显式入库） |
| `bar_adj_factor` | (market, symbol, trade_date) | 复权因子（adj_type: FACTOR/NONE） |
| `trade_calendar` | (market, cal_date) | 交易日历（按市场） |
| `whitelist_member` | (market, symbol) | 白名单（纯集合，无 included） |
| `sync_state` | (market, dataset, scope) | 增量水位（scope 恒 'WHITELIST'） |
| `staging_batch` | batch_id | staging 批次登记 |
| `task` | id | 任务（task_type/executor/market/handler/lock_key） |
| `task_log` | id | 任务日志 |
| `data_quality_issue` | 自然键(check_type, market, symbol, trade_date) | 质量问题 |
| `app_config` / `audit_log` | key / id | 配置（含 datasource.*）与审计 |

关系要点：
- 白名单成员可 JOIN instrument 取名称/类型；日 K 覆盖事实以 `bar_daily` 为准（**无 fetch_progress**）；
- 复权价查询时用 CTE（`repository.daily_query`），LEFT JOIN + anchor 兜底（无因子不丢行）；
- 元数据全量、日 K 仅白名单（incremental 强制；range 显式 symbols 豁免）。

## 3. 任务生命周期

```
POST /api/tasks {task_type, executor, market, params}
  → DataService.submit
      → ExecutorRegistry.validate_submission   # 串类型/越市场/缺参/非法 symbol → 400，不进队列
      → params 注入 market/executor
  → TaskCenter.submit
      → task 行 PENDING；lock_key=(task_type,market) 互斥
      → 并发闸门：Semaphore(max_concurrency) 有许可 → 工作线程；否则 _waiting 排队（不占线程）
  → handler（tasks.* / tasks_ak.*，按 source 绑定 Fetcher）
      → 抓取(网络IO, 限频) → staging.write_unit(created)
      → validate_unit（OHLC/日期/白名单合规/单位…）
      → 通过 → mark VALIDATED        # I-01：kill 后可复用
      → 全部单元完成 → commit_units（单事务 upsert + 标 applied；跨 batch，单写队列 _COMMIT_LOCK）
  → 仅 bars.incremental 且整次无失败 → sync_state.last_trade_date 推进到窗口最后开市日
  → 失败/被杀：文件保留（created/validated），重跑按 I-01~I-04 复用，DB 零污染
```

复用三分支（fetcher）：两 kind 全 applied → 跳过；两 kind 在（含 validated）→ 不重抓 + validated 入提交批次；覆盖不全 → 重抓。复用按 `task_type/executor/symbols` 全等隔离。

## 4. 执行器注册表扩展点（data/executors.py）

```python
ExecutorSpec(name, task_type, source, markets, handler, params_schema, implemented)
```
- 一个 executor = 一个 task_type + 一个数据源 + 一组市场；**1 executor 只绑 1 个 task_type**。
- `build_default_registry()`：tushare×5（CN_A）+ akshare×3（HK/US）；新增源/任务在此注册。
- `params_schema` 驱动前端动态表单（`GET /api/executors`）与后端校验（动态任务页）。
- handler 在 `data/tasks/handlers.py` 装配（`tasks.*` / `tasks_ak.*`）；新数据源：实现 `DataSource`（base.py 契约，方法带 market）+ 注册 executor + 装配 handler。

## 5. 数据源

| 源 | 市场 | 接口 | 复权 | 市值 |
|---|---|---|---|---|
| tushare | CN_A | stock_basic / daily / adj_factor / daily_basic / trade_cal | FACTOR | 有（meta 落 instrument_metric） |
| akshare | HK/US | Sina：stock_hk_hist_sina / stock_us_hist_sina / index_*_daily_sina；日历由指数日K推导 | NONE/1.0 | 无（白名单手工维护） |

限频：`fetch.rpm / meta_rpm` 令牌桶 + 服务端限频协商（协商值不被配置覆盖）；实测东财(EM)接口会触发 IP 限流 → 港美股走 Sina。详见 `DATA_SOURCES.md`。

## 6. CPU/并发限流（task_center.py）

- 并发上限 `task.max_concurrency=10`（Semaphore；超限 PENDING 排队，不占工作线程）。
- 单任务占空比节流：`ctx.checkpoint()` 内 `sleep = cpu × (1/budget − 1)`（`time.process_time()` 计量，IO 不计入）。
- 全局预算分摊：`budget = min(cpu_ratio, cpu_budget_ratio / active)`。
- DB 提交单写队列：`staging._COMMIT_LOCK`。
- 配置：`task.max_concurrency / cpu_ratio=0.10 / cpu_budget_ratio=0.50`。

## 7. 前端（零构建）

- `web/vendor/` 提供 React 18 + antd 5 + htm/echarts UMD；无 npm/打包链。
- `web/app.js`：ROUTES（`#/data/fetch` 即「任务提交」）、菜单、SSE（`/api/tasks/stream` → `yafip-task` 事件）、`Y.api/Y.qs/Y.useApi`。
- 页面注册 `Y.pages.xxx`：overview/datasource/data_fetch(任务提交)/data_explore/data_quality/data_whitelist/tasks/task_detail/system_settings/system_audit/system_about。
- 动态任务表单按 `GET /api/executors` 的 `params_schema` 渲染。

## 8. 错误码（api/responses.py）

`1001 config · 1002 datasource · 1003 token · 2001~2004 task · 3001 executor · 4001 参数/查询 · 5001 whitelist · 5002 mv_unit · 7001 staging · 9999 兜底`

## 9. 配置键（config.py DEFAULTS）

`server.port=8203 · task.max_workers=2 · task.max_concurrency=10 · task.cpu_ratio=0.10 · task.cpu_budget_ratio=0.50 · fetch.rpm=120 · fetch.meta_rpm=1 · fetch.timeout_sec=15 · fetch.retry_max=3 · data.start_date=2010-01-01 · data.include_delisted=1 · whitelist.metric=total_mv · whitelist.threshold_wan=500000.0 · staging.retention_days=30 · task.retention_days=90 · sse.heartbeat_sec=15`；`datasource.*` 仅「数据源配置」页读写。
