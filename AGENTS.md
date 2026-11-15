# AGENTS.md — YaFip AI 协作指南

> 面向 AI agent 的规则文件。给人看的快速说明见 `README.md`；架构见 `docs/ARCHITECTURE.md`；红线见 `docs/INVARIANTS.md`。
> 只写规则，不写散文。

## 1. 项目定位

- 本地多市场日 K 量化数据底座（A 股优先），面向优质标的的日 K 波段决策；支持数据查询、多源抓取、白名单、异步任务、审计；策略/回测为后续规划（未实现）。
- **不做**：实盘接入/下单（永久不做）、日内/高频、长期投研分析、例行任务（全部人工触发，无调度器）、多用户/鉴权/密码。

## 2. 命令

- 依赖安装：`uv sync`。**pyproject.toml 变更后必须 `uv lock`**（uv.lock 是唯一真相；`uv sync --frozen` 必须可复现）。
- 测试：`uv run pytest`；前端冒烟 `node tests/frontend_smoke.js`；真实接口冒烟 `YAFIP_TUSHARE_TOKEN=xxx uv run pytest -m slow`（无 token 自动 skip）。
- 运行：`start_yafip.bat start` / `./start_yafip.sh start`（start/stop/restart；`-b` 后台；`--force` 强停；`--no-browser`）。
  - 或 `uv run python -m yafip.server`；启停工具 `python -m yafip.cli start|stop|restart`。
- 默认端口 8203（`YAFIP_PORT` / 界面「系统设置」可改），页面 `http://127.0.0.1:8203`。
- 改动交付前必跑：`uv run pytest` + `node tests/frontend_smoke.js`。

## 3. 目录地图

```
src/yafip/
  server.py            # FastAPI 装配 + lifespan（build_context 单一装配点）
  cli.py               # start/stop/restart
  bootstrap.py         # 建库/版本守卫/迁移脚手架（initialize）
  config.py            # 配置（app_config 持久化；DEFAULTS/VALIDATORS/REMOVED_KEYS）
  runtime.py context.py
  api/                 # 路由层：routes_{dataquery,datasource,executors,fetch,overview,system,task,whitelist} + deps/responses
  core/                # db.py(线程本地连接+tx) schema.sql(13表权威DDL) task_center.py(状态机+限流) audit.py sse.py lifecycle.py cancel_token.py
  data/
    market.py          # 市场常量/符号规范化/infer_asset_type
    base.py            # DataSource 抽象 + 源侧列契约（ts_code…）
    repository.py      # 读写（含查询时复权 CTE）；行情写只经 staging
    staging.py         # created→validated→applied 状态机 + 校验器 + 单事务提交 + 复用/保留
    executors.py       # ExecutorSpec/Registry（扩展点）+ build_default_registry
    service.py         # DataService.submit（注册中心校验；注入 market/executor 到 params）
    tasks/             # handler：meta_sync_all/symbols、whitelist_update、bars_incremental/range（handlers.py 装配，按 source 绑定 Fetcher）
    fetcher.py         # 水位增量/区间抓取编排（复用三分支）
    whitelist.py       # 只进不出 + 读表规则
    quality.py         # 质量扫描（market 维度）
    tushare_source.py  # A股源（仅 CN_A；限频/退避）
    akshare_source.py  # 港美股源（Sina 为主；日历由指数日K推导）
    migrations.py      # 最小版本化迁移脚手架
web/                   # 零构建 UMD 前端（app.js + pages/*.js + vendor/）
tests/                 # pytest + frontend_smoke.js
docs/                  # 本文档 + ARCHITECTURE + INVARIANTS + DATA_SOURCES
data/                  # 运行时 DB/日志/staging（gitignore 根级锚定 /data/ /logs/，勿改动）
```

## 4. 核心约定（改代码前必读）

- 日期 `YYYY-MM-DD`；时间戳用 `core/db.now_iso()`；**日志/message/UI 一律中文**。
- 统一主键 `(market, symbol)`；`market ∈ {CN_A, HK, US}`；symbol 规范化只发生在 executor 边界（`data/market.py`）；`ts_code` 是源侧字段，不是主键。
- **入库铁律**：行情/元数据必须走 `staging → validate → commit` 三段式（`data/staging.py`）；禁止直接 INSERT/UPDATE 行情表。白名单/任务/审计等非行情表可直写。
- 任务模型：`task_type × executor`；executor 在 `data/executors.py` 注册（**一个 executor 只绑 1 个 task_type**）；handler 名 = `tasks.*`（tushare）/ `tasks_ak.*`（akshare），在 `data/tasks/handlers.py` 装配；提交经 `DataService.submit`（校验不符 400，不进队列）。
- 增量水位 `sync_state(market, dataset, scope='WHITELIST')`：整次成功才推进；`bars.range/full/repair` 永不推进；水位与白名单内容解耦。
- 复用/保留语义见 `docs/INVARIANTS.md` I-01~I-04（VALIDATED 状态必须被使用、复用按 task_type/executor/symbols 隔离、incremental 保留到水位覆盖）。
- 错误：业务异常带 `code` + `http_status`，挂到 `api/responses.install_error_handlers`（错误码见 ARCHITECTURE §错误码）。
- 配置：新键进 `config.DEFAULTS` + `VALIDATORS`；`datasource.*` 只经「数据源配置」页读写（/api/config 过滤、拒绝 set_many）。
- 迁移：`migrations.py` 最小脚手架（按 user_version 推进）；schema 变更=新迁移；旧库（`user_version < 10`）由 `init_schema` 版本守卫**直接报错并提示归档**，不迁移。
- 前端：零构建 UMD（React 18 + antd 5 + htm），页面注册到 `Y.pages`，路由在 `web/app.js` ROUTES；接口经 `Y.api`。

## 5. DO NOT 清单

- 不直接 `INSERT/UPDATE` `bar_daily / bar_adj_factor / instrument / instrument_metric`（走 staging）。
- 不引入 ORM / 消息队列 / 微服务 / 前端构建链 / 定时调度器。
- 不把 `task.business/action` 加回来；不加回 `ts_codes / enabled / included` 等已下线兼容字段。
- 不把 `.gitignore` 的 `/data/` `/logs/` 改回无锚定（`data/` 会吞掉 `src/yafip/data/`）。
- 不重建 `fetch_progress`（已删除；覆盖事实源 = bar 表 + staging + sync_state）。
- 不改 `core/schema.sql` 之外的表结构却不写迁移；不在 handler 里绕过 executor 注册中心。
- 不在 staging 校验通过后漏掉 `mark VALIDATED`（否则重跑会重复请求数据源）。
- 不新增例行/定时任务；任务一律 `source="manual"` 人工触发。
- 不把 token 写日志 / 环境变量 / /api/config 响应。

## 6. 协作偏好

- 中文沟通；结论先行（完成情况 / 剩余问题 / 下一步）。
- **技术问题直接修**（含文档矛盾）；仅个人偏好/口味类选择先向用户确认。
- 大块交付后用户可能「自己先看看」——此时停止推进，简短收尾。
- 功能开发必须走 SDD：`.comate/specs/{name}/doc.md → tasks.md → 逐任务执行 → summary.md`；每任务后跑 pytest，不许堆积未验证改动。
- 交付前自查：pytest 全绿 + frontend_smoke 通过；**本仓不引入 git**（版本管理由用户另行维护，勿初始化/操作版本控制）。

## 7. 文档索引

- `README.md` — 给人看的快速说明（启动/首次使用/边界）
- `docs/ARCHITECTURE.md` — 当前代码架构（分层/表/任务生命周期/扩展点）
- `docs/INVARIANTS.md` — 红线清单（验收对照）
- `docs/DATA_SOURCES.md` — 数据源接口摸底与扩展指南（新增数据源先读它）
