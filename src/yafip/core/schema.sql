-- yafip 权威 DDL（/ user_version 10）
-- 多市场数据底座：统一 (market, symbol) 二元主键；日 K 仅白名单；元数据全量。
-- 所有 CREATE 均带 IF NOT EXISTS，服务启动可重复执行（幂等）。
-- 日期统一 ISO：YYYY-MM-DD；时间戳统一 YYYY-MM-DDTHH:MM:SS。
-- 市场：CN_A / HK / US；symbol 规范化只在 executor 边界发生。

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA page_size    = 4096;

-- ---------------------------------------------------------------------------
-- 配置与审计
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 数据源配置（token 等）并入 app_config，key 空间 datasource.{name}.{field}。

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    actor  TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

-- ---------------------------------------------------------------------------
-- 元数据（全量，所有市场共用最小集；异构字段放 ext_json / instrument_metric）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instrument (
    market      TEXT NOT NULL,                -- CN_A / HK / US
    symbol      TEXT NOT NULL,                -- 内部规范化符号（如 600519.SH / 00700.HK / AAPL）
    name        TEXT,
    asset_type  TEXT NOT NULL,                -- STOCK / ETF / INDEX / VIX
    exchange    TEXT,
    currency    TEXT,
    list_date   TEXT,
    delist_date TEXT,
    status      TEXT,                         -- L 上市 / D 退市 / P 暂停
    ext_json    TEXT,                         -- 市场/类型特有字段 JSON，不打平
    source      TEXT,                         -- 数据源（tushare / akshare / ...）
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (market, symbol)
);
CREATE INDEX IF NOT EXISTS idx_instrument_type ON instrument(asset_type);

-- 异构指标（当前市值等；指数/VIX 无市值则不写行）
CREATE TABLE IF NOT EXISTS instrument_metric (
    market      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    metric_key  TEXT NOT NULL,                -- total_mv / pe / lots / ...
    metric_value REAL,
    unit        TEXT,
    as_of_date  TEXT,
    source      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (market, symbol, metric_key)
);

-- ---------------------------------------------------------------------------
-- 日 K（原始，权威）与复权因子（权威）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bar_daily (
    market     TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    pre_close  REAL,
    vol        REAL,
    amount     REAL,
    currency   TEXT,                          -- 显式币种，废除全局隐式约定
    vol_unit   TEXT,                          -- 股 / 手 / 份
    amount_unit TEXT,                         -- 元 / 千元 / 万美元 ...
    source     TEXT,                          -- 最近写入该行的数据源
    PRIMARY KEY (market, symbol, trade_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bar_date ON bar_daily(market, trade_date);

CREATE TABLE IF NOT EXISTS bar_adj_factor (
    market     TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    adj_factor REAL NOT NULL,
    adj_type   TEXT NOT NULL DEFAULT 'FACTOR', -- FACTOR / SPLIT / NONE（无因子市场）
    PRIMARY KEY (market, symbol, trade_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bar_adj_date ON bar_adj_factor(market, trade_date);

-- ---------------------------------------------------------------------------
-- 交易日历（按市场维度）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_calendar (
    market        TEXT NOT NULL,
    cal_date      TEXT NOT NULL,
    is_open       INTEGER NOT NULL,
    pretrade_date TEXT,
    PRIMARY KEY (market, cal_date)
);

-- ---------------------------------------------------------------------------
-- 白名单（纯集合：只进不出，删除只能人工；无 included 列）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whitelist_member (
    market      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    rule_key    TEXT,                         -- 命中规则（本期单条件，预留多规则）
    metric_value REAL,
    as_of_date  TEXT,                         -- 指标对应日期
    reason      TEXT,                         -- 人工维护说明
    source      TEXT,                         -- rule / manual
    computed_at TEXT NOT NULL,
    PRIMARY KEY (market, symbol)
);

-- ---------------------------------------------------------------------------
-- 同步水位（「上次更新日期」，与白名单内容解耦；scope 本期恒 'WHITELIST'）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    market         TEXT NOT NULL,
    dataset        TEXT NOT NULL,             -- bars / meta / ...
    scope          TEXT NOT NULL DEFAULT 'WHITELIST',
    last_trade_date TEXT,
    last_success_at TEXT,
    status         TEXT,
    PRIMARY KEY (market, dataset, scope)
);

-- ---------------------------------------------------------------------------
-- staging 批次登记（临时文件确认后再入库的审计；文件本体在 data/staging/）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging_batch (
    batch_id     TEXT PRIMARY KEY,
    task_id      TEXT,
    market       TEXT,
    dataset      TEXT,
    file_path    TEXT,
    rows         INTEGER DEFAULT 0,
    checksum     TEXT,
    status       TEXT NOT NULL,               -- created / validated / applied / failed
    validated_at TEXT,
    applied_at   TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 任务（新模型：task_type × executor × market）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task (
    id          TEXT PRIMARY KEY,
    task_type   TEXT NOT NULL,                -- meta.sync_all / meta.sync_symbols / whitelist.update / bars.incremental / bars.range
    executor    TEXT NOT NULL,                -- tushare.bars.incremental 等（注册中心驱动）
    market      TEXT,                         -- CN_A / HK / US / NULL（跨市场）
    source      TEXT NOT NULL,                -- manual / system
    lock_key    TEXT NOT NULL,                -- (task_type, market) 派生
    params      TEXT NOT NULL,
    handler     TEXT NOT NULL,                -- 提交时校验 executor.task_type == 请求 task_type
    status      TEXT NOT NULL,
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT,
    message     TEXT,
    error       TEXT,
    result      TEXT,
    retry_of    TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_status_created ON task(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_lock ON task(lock_key, status);
CREATE INDEX IF NOT EXISTS idx_task_type ON task(task_type, created_at DESC);

-- ---------------------------------------------------------------------------
-- 任务日志
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasklog_task ON task_log(task_id, id);

-- ---------------------------------------------------------------------------
-- 数据质量（自然键含市场维度）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_quality_issue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    check_type  TEXT NOT NULL,
    market      TEXT NOT NULL DEFAULT '',
    symbol      TEXT NOT NULL DEFAULT '',
    trade_date  TEXT NOT NULL DEFAULT '',
    detail      TEXT,
    detected_at TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    UNIQUE (check_type, market, symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_quality_unresolved ON data_quality_issue(resolved, detected_at DESC);

-- ---------------------------------------------------------------------------
-- 说明
-- 旧表 stock_basic / stock_daily / stock_adj_factor / fetch_progress /
-- datasource_config / schedule 不再建表；版本化迁移由最小迁移脚手架承载
-- （migrate_base_v10 占位，仅框架）。
-- 版本守卫：user_version < 10 的旧库在 init_schema() 中直接报错并提示归档。
-- ---------------------------------------------------------------------------

PRAGMA user_version = 10;
