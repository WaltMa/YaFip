"""抓取编排：水位增量 + staging 三段式入库。

- 原子单元 = 一个交易日；网络 IO 在事务外，入库经 staging（CSV + manifest）；
- 增量水位 = sync_state.last_trade_date（整次无失败才推进；range/full/repair 不动水位）；
- 重跑复用：已 applied/validated 的 staging 文件直接跳过，不重复请求数据源；
- 失败明细留在 task.result.failed_units，下次增量从原水位重扫（幂等 upsert）。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from yafip.config import ConfigService
from yafip.core.db import Database
from yafip.data.base import (
    DataSource,
    DataSourceError,
    FatalDataSourceError,
)
from yafip.data.market import MARKET_CN_A, MarketError, normalize_symbol, require_market
from yafip.data.repository import Repository
from yafip.data.staging import (
    DATASET_BARS,
    KIND_ADJ,
    KIND_BARS,
    StagingStore,
)
from yafip.data.tasks import market_units, resolve_adj_policy
from yafip.data.tasks.policy import market_closed, market_local_date
from yafip.data.whitelist import WhitelistError, WhitelistService

MODES = {"full", "incremental", "range", "repair"}
MAX_FAILED_DETAIL = 100
# 开市日相邻间隔超过该自然日数视为可疑空洞（长假 + 余量）；
# 更小空洞与休市不可区分，靠质量扫描 missing_days 兜底。
MAX_CAL_GAP_DAYS = 31


class FetchParamError(ValueError):
    code = "4001"


class Fetcher:
    def __init__(
        self,
        db: Database,
        repo: Repository,
        config: ConfigService,
        source: DataSource,
        whitelist: WhitelistService,
        staging: StagingStore,
    ):
        self.db = db
        self.repo = repo
        self.config = config
        self.source = source
        self.whitelist = whitelist
        self.staging = staging

    # ======================================================================
    # 入口
    # ======================================================================
    def run(self, ctx, params: dict[str, Any]) -> dict:
        started = time.monotonic()
        market = require_market(str(params.get("market") or MARKET_CN_A))
        mode = str(params.get("mode") or "incremental").lower()
        if mode not in MODES:
            raise FetchParamError(f"未知抓取模式：{mode}")

        today = date.today().isoformat()
        explicit = {normalize_symbol(market, str(c)) for c in (params.get("symbols") or []) if str(c).strip()}

        if explicit:
            codes = explicit
            if mode == "incremental":
                ctx.log("WARN", "增量任务带指定 symbols：仅抓取这些标的，且不推进整市场水位（其余白名单成员这段历史仍需覆盖）")
        else:
            codes = self.whitelist.state(market)
            if not codes:
                raise WhitelistError(
                    f"{market} 白名单为空：请先在「白名单管理」添加标的或运行白名单任务"
                )

        start, end, dates = self._plan(ctx, market, mode, params, today)

        # 任务上下文（单元 manifest 记录真实 task_type/executor，复用按此隔离）
        task_type = "bars.incremental" if mode == "incremental" else "bars.range"
        executor_name = str(params.get("executor") or f"{self.source.name}.{task_type}")

        # 重跑复用（仅 incremental）：三分支——
        #   两 kind 均 applied → 跳过（已落库）；
        #   两 kind 在（含 validated）→ 不重抓，validated 单元文件加入提交批次；
        #   覆盖不全 → 重抓。range/full/repair 是显式覆盖，一律重新抓取。
        reused_days = 0
        reuse_commit: list[tuple[Any, str]] = []
        fetch_dates = dates
        if mode == "incremental":
            units = self.staging.reusable_units(
                DATASET_BARS, market,
                kinds=(KIND_BARS, KIND_ADJ),
                task_type=task_type, executor=executor_name, symbols=codes,
            )
            by_date: dict[str, dict[str, dict]] = {}
            for u in units:
                by_date.setdefault(u["unit_key"], {})[u["kind"]] = u
            fetch_dates = []
            for d in dates:
                covered = by_date.get(d, {})
                if len(covered) >= 2 and all(u["status"] == "applied" for u in covered.values()):
                    reused_days += 1
                    continue
                if len(covered) >= 2:
                    # 复用文件（validated → 提交；applied → 已落库不用动）
                    for u in covered.values():
                        if u["status"] == "validated":
                            reuse_commit.append((Path(u["path"]), u["kind"]))
                    continue
                fetch_dates.append(d)

        ctx.log(
            "INFO",
            f"抓取开始：{market} {mode}，{len(fetch_dates)} 个交易日（复用跳过 {reused_days}，复用提交 {len(reuse_commit)}），标的 {len(codes)}",
        )
        if not fetch_dates and not reuse_commit:
            # 全复用（整窗口已 applied）：增量水位仍应推进到窗口最后开市日；
            # 但指定标的的部分抓取不推进整市场水位（其余白名单成员这段历史仍需覆盖）
            watermark_advanced = False
            if mode == "incremental" and not explicit and dates:
                self.repo.set_sync_state(market, dates[-1], status="ok")
                watermark_advanced = True
            ctx.progress(100, "完成", f"无需抓取（复用 {reused_days} 天）")
            return self._result(start, end, market, mode, 0, 0, 0, 0, 0, 0, reused_days, [], [], watermark_advanced, started)

        batch_id = ctx.task_id
        pending_units: list[tuple[Any, str]] = []
        done = failed = 0
        rows_daily = rows_adj = filtered = 0
        failed_units: list[dict] = []
        total_symbol_failed: dict[str, str] = {}  # symbol → 最近一次失败原因
        total = len(fetch_dates)

        for index, trade_date in enumerate(fetch_dates):
            ctx.checkpoint()
            outcome, unit_list, n_daily, n_adj, n_filtered, error, symbol_failed = self._process_unit(
                ctx, market, trade_date, codes, batch_id, task_type, executor_name
            )
            if symbol_failed:
                # akshare 单标的失败：不阻断整日，但必须写进任务日志与结果（否则缺失静默永久化）
                ctx.log(
                    "ERROR",
                    f"{trade_date} 部分失败 {len(symbol_failed)} 个标的："
                    + "; ".join(f"{f['symbol']}（{f['error']}）" for f in symbol_failed),
                )
                for f in symbol_failed:
                    total_symbol_failed[f["symbol"]] = f["error"]
            if outcome == "done":
                done += 1
                rows_daily += n_daily
                rows_adj += n_adj
                filtered += n_filtered
                pending_units.extend(unit_list)
            else:
                failed += 1
                # 失败路径不带可提交单元：adj 失败已把 bars 标 failed（整日回退），
                # 本次零入库、不推水位；下次增量从原水位重扫重抓整日
                if len(failed_units) < MAX_FAILED_DETAIL:
                    failed_units.append({"trade_date": trade_date, "error": error})
                ctx.log("ERROR", f"{trade_date} 抓取失败：{error}")

            if index % 20 == 0 or index == total - 1:
                ctx.progress(
                    20 + 65 * (index + 1) / total,
                    f"抓取 {trade_date}",
                    f"完成 {done} / 失败 {failed}",
                )
                ctx.log("INFO", f"进度 {index + 1}/{total}：{trade_date}，完成 {done}，失败 {failed}")

        # 整次提交（单事务）：新抓单元 + 复用 validated 单元（跨 batch，）
        all_units = pending_units + reuse_commit
        ctx.progress(90, "提交入库", f"{len(all_units)} 个单元")
        ctx.checkpoint()
        committed = 0
        if all_units:
            adj_type, _fallback = resolve_adj_policy(market)
            currency, vol_unit, amount_unit = market_units(market)
            committed = self.staging.commit_units(
                DATASET_BARS, market, all_units,
                source=self.source.name,
                adj_policy=(adj_type, None),
                currency=currency, vol_unit=vol_unit, amount_unit=amount_unit,
            )

        watermark_advanced = False
        if mode == "incremental" and not explicit and not failed_units and dates:
            # 推进到规划窗口最后一个开市日（含重跑复用的日期）；
            # 指定标的的部分抓取不推进整市场水位
            self.repo.set_sync_state(market, dates[-1], status="ok")
            watermark_advanced = True

        ctx.progress(100, "完成", f"完成 {done}，失败 {failed}，提交 {committed} 个单元")
        failed_symbols = (
            [{"symbol": symbol, "error": err} for symbol, err in total_symbol_failed.items()]
            if total_symbol_failed else []
        )
        return self._result(
            start, end, market, mode, done, failed, rows_daily, rows_adj, filtered,
            committed, reused_days, failed_units, failed_symbols, watermark_advanced, started,
        )

    def _result(
        self, start, end, market, mode, done, failed, rows_daily, rows_adj, filtered,
        committed, reused_days, failed_units, failed_symbols, watermark_advanced, started,
    ) -> dict:
        result = {
            "market": market,
            "mode": mode,
            "start": start,
            "end": end,
            "dates_total": done + failed + reused_days,
            "dates_done": done,
            "dates_failed": failed,
            "dates_reused": reused_days,
            "rows_daily": rows_daily,
            "rows_adj": rows_adj,
            "filtered_rows": filtered,
            "units_committed": committed,
            "failed_units": failed_units,
            "watermark_advanced": watermark_advanced,
            "elapsed_sec": round(time.monotonic() - started, 1),
        }
        if failed_symbols:
            # 部分标的失败（如 akshare 单标的接口异常）：任务详情可见，缺失需 range/repair 补缺
            result["failed_symbols"] = failed_symbols
        if not failed_units:
            result["message"] = "抓取完成"
        return result

    # ======================================================================
    # 规划
    # ======================================================================
    def _plan(
        self,
        ctx,
        market: str,
        mode: str,
        params: dict,
        today: str,
    ) -> tuple[str, str, list[str]]:
        start_date = self.config.get_str("data.start_date", "2010-01-01")

        if mode == "incremental":
            watermark = self.repo.sync_state_watermark(market)
            start = watermark or start_date
            if watermark:
                start = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
            # 增量上限 = 市场本地“今天”：US 交易日相对服务器日期有偏移，不能用服务器 today-1
            # 推导（会把正在交易的美股日期当作已收盘）。已收盘纳入今天；未收盘截止到市场本地
            # 昨日，最终由 latest_open_date 收敛到最近一个已收盘的开市日。
            end = market_local_date(market)
            if not market_closed(market):
                end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
                ctx.log("INFO", "市场尚未收盘，增量截止到市场本地昨日（避免盘中快照入库）")
        elif mode == "range":
            if not params.get("start") or not params.get("end"):
                raise FetchParamError("range 模式必须提供 start 与 end")
            start = str(params["start"])[:10]
            end = str(params["end"])[:10]
        elif mode == "repair":
            end = str(params.get("end") or today)[:10]
            default_start = (date.fromisoformat(today) - timedelta(days=90)).isoformat()
            start = str(params.get("start") or default_start)[:10]
        else:  # full
            start = str(params.get("start") or start_date)[:10]
            end = str(params.get("end") or today)[:10]

        if start > end:
            raise FetchParamError(f"开始日期晚于结束日期：{start} > {end}")

        self._ensure_calendar(ctx, market, min(start, start_date), max(end, today))

        if mode == "repair" and params.get("dates"):
            return start, end, sorted({str(d)[:10] for d in params["dates"] if str(d).strip()})

        latest = self.repo.latest_open_date(market, end)
        if latest is None or start > latest:
            return start, end, []
        return start, end, self.repo.open_dates(market, start, latest)

    # ======================================================================
    # 单元
    # ======================================================================
    def _process_unit(
        self,
        ctx,
        market: str,
        trade_date: str,
        codes: set[str],
        batch_id: str,
        task_type: str,
        executor_name: str,
    ) -> tuple[str, list[tuple[Any, str]], int, int, int, str | None, list[dict]]:
        ctx.checkpoint()
        try:
            daily = self.source.get_daily(market, trade_date, symbols=sorted(codes))
        except FatalDataSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单元失败不阻断整体
            return "failed", [], 0, 0, 0, f"{type(exc).__name__}: {exc}", []

        # akshare 等源把单标的失败挂在结果 attrs 上（部分成功）；读取后透传到任务结果
        symbol_failed = list((daily.attrs or {}).get("failed_symbols") or [])

        if daily is None or daily.empty:
            # 全部标的失败 → 空结果：按“当日失败”处理，不产生 failed_symbols（日级语义已覆盖）
            return "failed", [], 0, 0, 0, "daily 返回空结果（当日数据未发布或接口不可用）", []

        daily_mapped, n_filtered = self._map_filter(daily, market, codes)
        if daily_mapped is None or daily_mapped.empty:
            # 源有数据但 0 命中目标集合：不再当作成功推进水位——
            # 可能是 symbol 格式不对或源不覆盖，静默覆盖会让这段历史永久跳过。
            return (
                "failed", [], 0, 0, n_filtered,
                f"源返回 {len(daily)} 行但 0 命中目标集合（{len(codes)} 个标的），"
                "请检查 symbol 格式与源覆盖（白名单含源不覆盖标的时增量会持续失败，移除后恢复）",
                [],
            )

        units: list[tuple[Any, str]] = []
        rows_daily = rows_adj = 0
        adj_type, adj_fallback = resolve_adj_policy(market)

        # bars 单元
        bars_csv = self.staging.write_unit(
            batch_id=batch_id, task_id=ctx.task_id, task_type=task_type,
            executor=executor_name, source=self.source.name,
            market=market, dataset=DATASET_BARS, unit_key=trade_date, kind=KIND_BARS,
            df=daily_mapped, params_snapshot={"mode": "bars"},
        )
        errors = self.staging.validate_unit(
            bars_csv, market=market, kind=KIND_BARS,
            start=trade_date, end=trade_date,
        )
        if errors:
            self.staging.mark_unit_status(
                DATASET_BARS, market, batch_id, bars_csv.name, "failed", error="; ".join(errors)[:500],
            )
            return "failed", [], 0, 0, n_filtered, f"bars 校验失败：{'；'.join(errors)}", []
        # 校验通过立即标 VALIDATED——kill 于 commit 前时，重跑可复用文件
        self.staging.mark_unit_status(DATASET_BARS, market, batch_id, bars_csv.name, "validated")
        units.append((bars_csv, KIND_BARS))
        rows_daily = int(len(daily_mapped))
        ctx.checkpoint()

        # adj 单元
        def _rollback_bars(reason: str) -> None:
            """adj 失败 → 整日回退：已写 bars 单元标 failed（不提交），避免半截数据。"""
            self.staging.mark_unit_status(
                DATASET_BARS, market, batch_id, bars_csv.name, "failed",
                error=f"adj 失败导致整日回退：{reason}"[:500],
            )

        if adj_type == "NONE":
            # 无因子市场（HK/US）：写 NONE/1.0 占位行，不依赖数据源因子接口
            adj_df = daily_mapped[["symbol"]].copy()
            adj_df["trade_date"] = trade_date
            adj_df["adj_factor"] = adj_fallback
            adj_present = True
        else:
            try:
                adj_df = self.source.get_adj_factor(market, trade_date)
            except FatalDataSourceError:
                raise
            except Exception as exc:  # noqa: BLE001
                _rollback_bars(f"{type(exc).__name__}: {exc}")
                return "failed", [], 0, 0, n_filtered, f"{type(exc).__name__}: {exc}", []
            adj_mapped, _ = self._map_filter(adj_df, market, codes)
            if adj_df is None or adj_df.empty or adj_mapped is None or adj_mapped.empty:
                _rollback_bars("adj_factor 返回空结果")
                return "failed", [], 0, 0, n_filtered, (
                    "adj_factor 返回空结果（因子未发布或积分/权限不足），整日已回退待重扫"
                ), []
            adj_df = adj_mapped
            adj_present = True

        adj_csv = self.staging.write_unit(
            batch_id=batch_id, task_id=ctx.task_id, task_type=task_type,
            executor=executor_name, source=self.source.name,
            market=market, dataset=DATASET_BARS, unit_key=trade_date, kind=KIND_ADJ,
            df=adj_df, params_snapshot={"mode": "bars"},
        )
        errors = self.staging.validate_unit(
            adj_csv, market=market, kind=KIND_ADJ,
            start=trade_date, end=trade_date, adj_policy=(adj_type, adj_fallback),
        )
        if errors:
            self.staging.mark_unit_status(
                DATASET_BARS, market, batch_id, adj_csv.name, "failed", error="; ".join(errors)[:500],
            )
            _rollback_bars("adj 校验失败")
            return "failed", [], 0, 0, n_filtered, f"adj 校验失败：{'；'.join(errors)}", []
        self.staging.mark_unit_status(DATASET_BARS, market, batch_id, adj_csv.name, "validated")
        units.append((adj_csv, KIND_ADJ))
        rows_adj = int(len(adj_df))
        ctx.checkpoint()
        return "done", units, rows_daily, rows_adj, n_filtered, None, symbol_failed

    @staticmethod
    def _map_filter(
        df: pd.DataFrame, market: str, codes: set[str]
    ) -> tuple[pd.DataFrame, int]:
        """executor 边界：ts_code → (market, symbol)，并过滤目标集合。"""
        if df is None or df.empty:
            return df, 0
        temp = df.copy()
        symbols: list[str | None] = []
        filtered = 0
        for raw in temp["ts_code"]:
            try:
                symbol = normalize_symbol(market, raw)
            except MarketError:
                symbol = None
            if symbol is None or symbol not in codes:
                filtered += 1
            symbols.append(symbol)
        temp["symbol"] = symbols
        temp = temp[temp["symbol"].notna() & temp["symbol"].isin(codes)]
        return temp, filtered

    # ======================================================================
    # 准备
    # ======================================================================
    def _ensure_calendar(self, ctx, market: str, start: str, end: str) -> None:
        min_date = self.db.scalar("SELECT MIN(cal_date) FROM trade_calendar WHERE market=?", (market,))
        max_date = self.db.scalar("SELECT MAX(cal_date) FROM trade_calendar WHERE market=?", (market,))
        if min_date and max_date and str(min_date) <= start and str(max_date) >= end:
            # 覆盖检查通过不代表中间完整：源侧停更/人为删行会留下首尾覆盖的大空洞。
            # 仅抓可检测的大间隔（>31 自然日），小空洞靠质量扫描兜底。
            gap = self._max_calendar_gap(market, start, end)
            if gap > MAX_CAL_GAP_DAYS:
                ctx.log(
                    "WARN",
                    f"交易日历疑似空洞：{market} {start} ~ {end} 相邻开市日最大间隔 {gap} 天"
                    f"（阈值 {MAX_CAL_GAP_DAYS} 天），重拉整窗补齐",
                )
                self._fetch_calendar(ctx, market, start, end)
            return

        fetch_start = start
        fetch_end = end
        if min_date and str(min_date) <= start and max_date and str(max_date) < end:
            fetch_start = (date.fromisoformat(str(max_date)) + timedelta(days=1)).isoformat()
        if max_date and str(max_date) >= end:
            fetch_end = end

        self._fetch_calendar(ctx, market, fetch_start, fetch_end)

    def _max_calendar_gap(self, market: str, start: str, end: str) -> int:
        """窗口内相邻开市日的最大自然日间隔；不足两个开市日视为无空洞。"""
        days = [date.fromisoformat(d) for d in self.repo.open_dates(market, start, end)]
        if len(days) < 2:
            return 0
        return max((right - left).days for left, right in zip(days, days[1:]))

    def _fetch_calendar(self, ctx, market: str, fetch_start: str, fetch_end: str) -> None:
        ctx.log("INFO", f"更新交易日历：{market} {fetch_start} ~ {fetch_end}")
        try:
            df = self.source.get_calendar(market, fetch_start, fetch_end)
        except FatalDataSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataSourceError(f"交易日历获取失败：{exc}") from exc
        self.repo.upsert_calendar(market, df.to_dict("records"))
