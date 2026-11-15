# 数据源接口摸底与扩展指南

> 实测结论（akshare 1.20.64）。新增/替换数据源执行器前先读本文。

## 1. 现有数据源

| 源 | 市场 | token | 日 K | 元数据 | 日历 | 复权 | 市值 |
|---|---|---|---|---|---|---|---|
| tushare | CN_A | 需要 | `daily` | `stock_basic`（仅股票） | `trade_cal(SSE)` | `adj_factor` → FACTOR | `daily_basic` → instrument_metric |
| akshare | HK/US | 无需 | Sina 系列 | `stock_hk_spot_sina` / `stock_us_name_sina` | 由指数日K推导 | 无 → NONE/1.0 | 无（白名单手工） |

## 2. akshare 接口实测（列名与单位）

- 港股个股日K `stock_hk_hist_sina(symbol='00700', period='daily', adjust='')`：
  `date/open/high/low/close/volume/amount`（volume 股、amount 港元）
- 美股个股日K `stock_us_hist_sina(symbol='AAPL', period='daily', adjust='')`：
  `date/open/high/low/close/volume`（**无 amount**；含非交易日 NaN 占位行，适配器按 close 过滤）
- 指数日K：HK `index_hk_daily_sina(symbol='CES100')`；US `index_us_daily_sina(symbol='.INX')`（**仅 .IXIC/.DJI/.INX/.NDX，无 VIX**）
- 元数据：HK `stock_hk_spot_sina()` → `代码/中文名称/英文名称`（分页慢）；US `stock_us_name_sina()` → `name/cname/symbol`（轻量；US 全量同步仍分钟级）
- A股日历：`calendar_trade_date_hist_sina()`

## 3. 实测风险

- **东财(EM)接口限流严格**：`push2 行情服务器断开，IP 临时限流约 5 分钟，反复触发 40 分钟+` → 港美股以 Sina 为主路径；接入 EM 接口（如 `index_global_hist_em`）需独立限频并容忍惩罚。
- tushare 服务端限频协商值**不被配置覆盖**（`_server_rates`）；`daily_basic` 积分门槛通常高于 `daily`。

## 4. 扩展一个新数据源（步骤）

1. 实现 `DataSource`（`data/base.py` 契约，方法均带 `market`；`get_daily(market, date, symbols=None)`）；
2. `data/registry.py` SOURCES 登记（数据源配置页可见；免 token 的 `requires_token=False`）；
3. `data/executors.py` 注册 `ExecutorSpec(name, task_type, source, markets, handler, params_schema, implemented)`（**1 executor = 1 task_type**）；
4. `data/tasks/handlers.py` 装配 handler（按 source 绑定独立 Fetcher）；
5. `server.py build_context` 接入源实例；`routes_datasource` 按名解析（`ctx.source` / `ctx.akshare_source`）；
6. 复权策略走 `data/tasks/policy.py resolve_adj_policy`（CN_A→FACTOR，其余→NONE/1.0）；单位走 `market_units`。

## 5. 待接入清单（机制已就位）

- A股 ETF（`fund_basic`）/ 指数（`index_daily`）——tushare 接口待接线；资产类型已按代码段分类（`infer_asset_type`）。
- 美股 VIX（`^VIX`）——Sina 无接口；可选 `index_global_hist_em`（限流风险）。
- 港美股市值接口——评估 akshare 估值类接口数据质量后再定。
- HK/US 未来交易日历——需 HKEX/NYSE 日历数据源（当前由指数日K推导历史日）。
