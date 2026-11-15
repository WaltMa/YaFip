# YaFip 红线清单（INVARIANTS）

> 改代码/评审时逐条对照；违反即缺陷。每条附「检查方式」。

| # | 红线 | 检查方式 |
|---|---|---|
| I-01 | 入库必须过 staging 三段式（staging → validate → commit）；禁止直接 INSERT/UPDATE 行情表 | `grep "INSERT INTO bar_daily|instrument" src/yafip --include=*.py` 应只见 staging.py 的 commit 路径；Repository 的 upsert_* 仅被 staging 调用 |
| I-02 | `(market, symbol)` 复合主键；`ts_code` 只是源侧字段 | schema.sql 各业务表主键；repository 查询均带 market |
| I-03 | 白名单只进不出：无 `included`/`enabled`/整表替换；删除仅 Web 人工 + audit | schema.sql whitelist_member 无 included；whitelist.py 无 DELETE 全表/`included=0` 写入 |
| I-04 | 水位与白名单内容解耦；`bars.range/full/repair` 永不推进 `sync_state` | fetcher：`set_sync_state` 仅在 `mode=="incremental"` 分支出现 |
| I-05 | 一个 executor 只绑 1 个 task_type；提交校验不符直接 400 不进队列 | executors.py ExecutorRegistry.validate_submission；test_executors.py |
| I-06 | `.gitignore` 的 `/data/`、`/logs/` 必须根级锚定（禁止无前导斜杠） | 重新引入版本管理后：`git check-ignore -v src/yafip/data/fetcher.py` 应返回空；root `data/` 仍被忽略 |
| I-07 | 旧库（`user_version<10`）直接报错并提示归档，不迁移 | db.init_schema 版本守卫；test_migrations.py 的 SchemaVersionError 用例 |
| I-08 | token 只存本地 SQLite（app_config `datasource.*`），页面维护；不出现在 /api/config，不走环境变量 | config.py DATASOURCE_KEY_PREFIX 过滤；test_datasource_api 断言 |
| I-09 | 无实盘 / 无日内 / 无策略回测（后续规划）；任务全人工触发（无调度器） | `grep -ri "apscheduler" src` 为空；task.source 仅 manual/system |
| I-10 | VALIDATED 状态必须被使用（校验通过即标；kill 后重跑复用） | fetcher/meta 校验通过后均有 `mark_unit_status(...,"validated")`；test_staging VALIDATED 生命周期用例 |
| I-11 | 复用必须按 `task_type/executor/symbols` 隔离；range 文件不得被 incremental 误用 | staging.reusable_units 过滤参数；test_fetcher::test_incremental_does_not_reuse_range_files |
| I-12 | 保留策略：incremental applied 保留到水位覆盖（无水位也保留）；range applied 即清 | staging.cleanup 分支；test_staging cleanup 三态用例 |
| I-13 | pyproject 变更后必须 `uv lock`（uv.lock 与依赖一致，`--frozen` 可复现） | `uv lock --check`；uv.lock 含 akshare |
| I-14 | CPU 限流：单任务 ≤10% 占空比、并发 ≤10、全局预算分摊；DB 提交单写队列 | task_center 常量/current_budget/_COMMIT_LOCK；test_cpu_throttle/test_concurrency |
| I-15 | 数据写入必须显式落 `currency/vol_unit/amount_unit`（`market_units`），无全局隐式单位约定 | fetcher commit 传 market_units；bar_daily 列非空 |
| I-16 | 前端零构建（UMD），不引入 npm/打包链 | web/vendor UMD；无 package.json 构建步骤 |

