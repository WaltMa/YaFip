# YaFip

**Yet Another Financial Independence Platform** —— 本地运行的量化数据底座：多市场（A股/港股/美股）日 K + 复权因子 + 白名单 + 人工触发的任务系统。面向优质标的的日 K 波段决策，**不做实盘、不做日内高频**；策略训练与简单回测为后续规划（未实现）。

> 当前代码为多市场量化数据底座（初版）。AI 协作规则见 `AGENTS.md`（Claude 等自动读取 `CLAUDE.md`）；架构/红线/数据源见 `docs/ARCHITECTURE.md`、`docs/INVARIANTS.md`、`docs/DATA_SOURCES.md`。

## 功能一览

- **多市场数据**：A 股（tushare）、港美股（akshare）——元数据全量、日 K 仅白名单；`(market, symbol)` 唯一标识标的
- **日 K 双表**：`bar_daily`（原始）+ `bar_adj_factor`（复权因子），前复权/后复权查询时计算
- **白名单只进不出**：规则任务按市值指标只增不删；删除仅页面人工并记审计
- **任务系统**：全人工触发（无例行调度）；staging 三段式入库（临时文件校验后单事务提交，断点可续）；并发上限 10、单任务 ≤10% CPU
- **Web 全操作**：任务提交、数据浏览、质量扫描、审计日志；无密码（仅本机 127.0.0.1）

## 快速开始

```bash
# Windows
start_yafip.bat start        # 后台：start --background；停止：stop [--force]
# macOS / Linux
./start_yafip.sh start

# 或直接
uv sync
uv run python -m yafip.server
```

浏览器打开 `http://127.0.0.1:8203`（端口可在「系统设置」或 `YAFIP_PORT` 环境变量修改）。

**首次使用（A 股）：**
1. 「数据源配置」录入 tushare token → 测试连接（token 只存本地 SQLite）
2. 「任务提交」→ 元数据全量同步（CN_A）→ 核对市值单位（万元）
3. 「白名单管理」→ 更新白名单（市值门槛，只进不出）或手工新增标的
4. 「任务提交」→ 日K增量同步（首次从 2010 年回填）→ 之后每日增量
5. 「数据浏览」按市场查原始/前复权/后复权行情

> ⚠️ 首次全量前建议**静置约 1 小时**：tushare 限频惩罚会把接口压到 1 次/小时。
> 港美股（akshare）无需 token：白名单手工加标的（如 00700.HK / AAPL）→ 日K区间同步即可，样例已跑通。

## 后续方向（规划）

以下为后续迭代的重要参考，当前未实现：

- **策略训练与简单回测**：数据底座已预留接口（`instrument_metric` 的 point-in-time 口径、`task_type` 可扩展 `strategy.*` 执行器）；属后续规划。
- **港美股执行器扩展**：当前已支持个股/指数日K（00700.HK / AAPL 样例跑通）；ETF、更多指数、VIX、港美股市值、完整未来交易日历待接入（机制就位，见 `docs/DATA_SOURCES.md`）。
- **A 股 ETF/指数数据源**：如 513100（ETF）、000688（指数）的元数据与日K接口待接入（`infer_asset_type` 已能正确分类）。

## 数据源

| 源 | 市场 | 说明 |
|---|---|---|
| tushare | A 股 | 需 token；日K/复权/市值/日历 |
| akshare | 港股/美股 | 免 token；日K/指数/标的（日历由指数日K推导，未来日未知） |

已知边界：A股 ETF/指数（如 513100、000688）与美股 VIX 的数据源接口待接入（schema 与资产类型分类已就位）；港美股无市值接口，白名单靠手工维护。详见 `docs/DATA_SOURCES.md`。

## 关键约定

- 主键 `(market, symbol)`：CN_A（600519.SH / 513100.SH / 000688.SH）、HK（00700.HK）、US（AAPL / QQQ）
- 日期 `YYYY-MM-DD`；单位显式落库（CNY/手/千元、HKD/股/港元、USD/股/美元）
- 增量水位 `sync_state`，与白名单内容解耦；指定同步（range）不推进水位
- 旧库（user_version<10）启动即报错并提示归档，不迁移

## 环境变量

数据源配置全部在页面维护（不用环境变量）。仅运行时参数可选覆盖：`YAFIP_PORT`（`.env` 可写）、`YAFIP_HOME` / `YAFIP_ENV_FILE`（须真实环境变量，`.env` 无效）。

## 运维与验证

- **停止语义**：`stop` 为平滑停止（停止接收新任务、等待运行中任务结束后退出）；`stop --force` 取消运行中任务尽快退出（超时兜底）。
- **限频**：tushare 服务端限频惩罚（短时间高频触发会压到 1 次/分钟甚至 1 次/小时）——首次全量前静置约 1 小时；协商出的真实限额不被配置覆盖。akshare 港美股走 Sina（东财接口会触发 IP 限流）。
- **staging 清理**：`/api/system/cleanup` 按 `staging.retention_days=30` 清理；规则——incremental 已提交且水位未覆盖其交易日的文件保留，range 已提交即清，校验未提交（validated）保留供续传。
- **断点续传**：任务被杀/失败后重跑自动复用 staging 文件（按任务类型/执行器/标的集合隔离），不重复请求数据源。
- **旧库**：user_version<10 的旧库启动即报错并提示归档到 `data/archive/`；归档后重启建全新库，token 需在页面重新录入。
- **A 股真实数据验证流程**：录 token → 测试连接 → 元数据全量同步（核对市值单位万元）→ 白名单更新 → 日K增量（首次回填）→ 日K区间（幂等、水位不变）→ 数据浏览核对 qfq/hfq → 60s 采样核对单任务 CPU ≤10%（限流验收）。

## 开发

```bash
uv sync && uv run pytest && node tests/frontend_smoke.js
uv run python -m yafip.cli start --background   # 启停工具
uv run python -m yafip.cli stop
```

文档索引：`AGENTS.md`（AI 协作）· `docs/ARCHITECTURE.md`（架构）· `docs/INVARIANTS.md`（红线）· `docs/DATA_SOURCES.md`（数据源）。
