"""meta.* 任务 handler：按市场全量 / 指定标的同步元数据与市值指标。

写入路径：staging（meta 批次）→ validate → commit（单事务）。
指数/ETF 无市值时不写 instrument_metric 行（不强制打平）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from yafip.config import ConfigService
from yafip.data.base import DataSource, DataSourceError, FatalDataSourceError
from yafip.data.market import MARKET_CN_A, MarketError, infer_asset_type, normalize_symbol, require_market
from yafip.data.repository import Repository
from yafip.data.staging import (
    DATASET_META,
    KIND_INSTRUMENT,
    KIND_METRIC,
    StagingStore,
)

UNIT_KEY = "meta"
METRIC_KEY_TOTAL_MV = "total_mv"


def _ensure_latest_trade_date(ctx, market: str, source: DataSource, repo: Repository) -> str:
    today = date.today().isoformat()
    latest = repo.latest_open_date(market, today)
    if latest is None:
        start = (date.today() - timedelta(days=60)).isoformat()
        ctx.log("INFO", f"交易日历为空，拉取 {start} ~ {today}")
        df = source.get_calendar(market, start, today)
        repo.upsert_calendar(market, df.to_dict("records"))
        latest = repo.latest_open_date(market, today)
    if latest is None:
        raise DataSourceError("交易日历为空，无法确定最新交易日")
    return str(latest)


def _map_codes(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """ts_code → symbol（executor 边界）。"""
    if df is None or df.empty:
        return df
    temp = df.copy()
    symbols = []
    for raw in temp["ts_code"]:
        try:
            symbols.append(normalize_symbol(market, str(raw)))
        except MarketError:
            symbols.append(None)
    temp["symbol"] = symbols
    return temp[temp["symbol"].notna()]


def _build_instrument_records(df: pd.DataFrame, market: str) -> list[dict]:
    records = []
    for r in df.to_dict("records"):
        records.append({
            "symbol": r["symbol"],
            "name": r.get("name"),
            # 源显式提供的 asset_type 优先；否则按市场代码规则推断（至少区分 STOCK/ETF/INDEX）
            "asset_type": str(r.get("asset_type") or infer_asset_type(market, r["symbol"])).upper(),
            "exchange": r.get("exchange"),
            "currency": r.get("currency"),
            "list_date": r.get("list_date"),
            "delist_date": r.get("delist_date"),
            "status": r.get("list_status") or r.get("status"),
            "ext_json": None,
        })
    return records


def _build_metric_records(df: pd.DataFrame, as_of_date: str) -> list[dict]:
    records = []
    for r in df.to_dict("records"):
        value = r.get("total_mv")
        if value is None or pd.isna(value):
            continue  # 指数/ETF 无市值不写 metric 行
        records.append({
            "symbol": r["symbol"],
            "metric_key": METRIC_KEY_TOTAL_MV,
            "metric_value": float(value),
            "unit": "万元",
            "as_of_date": as_of_date,
        })
    return records


def _sync(
    ctx,
    *,
    config: ConfigService,
    repo: Repository,
    source: DataSource,
    staging: StagingStore,
    params: dict[str, Any],
    symbols: set[str] | None,
) -> dict:
    market = require_market(str(params.get("market") or MARKET_CN_A))
    batch_id = ctx.task_id
    task_type = str(params.get("task_type") or "meta.sync_all")
    executor_name = str(params.get("executor") or f"{source.name}.{task_type}")

    ctx.progress(5, "准备交易日历", market)
    as_of = _ensure_latest_trade_date(ctx, market, source, repo)
    ctx.checkpoint()

    ctx.progress(20, "获取标的基本信息", market)
    include_delisted = config.get_bool("data.include_delisted", True)
    try:
        df_stocks = source.get_stock_list(market, include_delisted=include_delisted)
    except FatalDataSourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError(f"标的列表获取失败：{exc}") from exc
    if df_stocks is None or df_stocks.empty:
        raise DataSourceError("标的列表为空：请检查数据源 token 与积分")
    mapped = _map_codes(df_stocks, market)
    if symbols:
        mapped = mapped[mapped["symbol"].isin(symbols)]
    if mapped.empty:
        raise DataSourceError("指定标的未在标的列表中找到，请检查 symbol 与市场")
    ctx.checkpoint()

    ctx.progress(45, "获取市值指标", f"{as_of}")
    try:
        df_mv = source.get_daily_basic(market, as_of)
    except FatalDataSourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError(f"市值指标获取失败：{exc}") from exc
    mv = _map_codes(df_mv, market) if df_mv is not None else df_mv
    if symbols and mv is not None and not mv.empty:
        mv = mv[mv["symbol"].isin(symbols)]
    ctx.checkpoint()

    instrument_records = _build_instrument_records(mapped, market)
    metric_records = _build_metric_records(mv, as_of) if mv is not None else []

    ctx.progress(70, "写入 staging", f"instrument={len(instrument_records)} metric={len(metric_records)}")
    inst_csv = staging.write_unit(
        batch_id=batch_id, task_id=ctx.task_id, task_type=task_type,
        executor=executor_name, source=source.name, market=market,
        dataset=DATASET_META, unit_key=UNIT_KEY, kind=KIND_INSTRUMENT,
        df=pd.DataFrame(instrument_records) if instrument_records else pd.DataFrame(columns=["symbol"]),
        params_snapshot=dict(params),
    )
    units = [(inst_csv, KIND_INSTRUMENT)]
    if metric_records:
        metric_csv = staging.write_unit(
            batch_id=batch_id, task_id=ctx.task_id, task_type=task_type,
            executor=executor_name, source=source.name, market=market,
            dataset=DATASET_META, unit_key=UNIT_KEY, kind=KIND_METRIC,
            df=pd.DataFrame(metric_records),
            params_snapshot=dict(params),
        )
        units.append((metric_csv, KIND_METRIC))

    ctx.progress(85, "校验 staging")
    for csv, kind in units:
        errors = staging.validate_unit(csv, market=market, kind=kind)
        if errors:
            staging.mark_unit_status(DATASET_META, market, batch_id, csv.name, "failed", error="; ".join(errors)[:500])
            raise DataSourceError(f"staging 校验失败：{'；'.join(errors)}")
        # 校验通过标 VALIDATED，kill 后重跑可复用
        staging.mark_unit_status(DATASET_META, market, batch_id, csv.name, "validated")

    ctx.progress(95, "提交入库")
    staging.commit_units(DATASET_META, market, units, source=source.name)

    ctx.progress(100, "完成", f"instrument={len(instrument_records)} metric={len(metric_records)}")
    return {
        "market": market,
        "as_of_date": as_of,
        "instrument_rows": len(instrument_records),
        "metric_rows": len(metric_records),
        "batches": [batch_id],
        "message": "元数据同步完成",
    }


def make_meta_handlers(*, config, repo, source, staging, prefix: str = "tasks"):
    def meta_sync_all(ctx, params: dict) -> dict:
        return _sync(ctx, config=config, repo=repo, source=source, staging=staging,
                     params=params, symbols=None)

    def meta_sync_symbols(ctx, params: dict) -> dict:
        symbols = {str(s).strip().upper() for s in (params.get("symbols") or []) if str(s).strip()}
        if not symbols:
            from yafip.data.executors import ExecutorError

            raise ExecutorError("缺少必填参数：symbols")
        return _sync(ctx, config=config, repo=repo, source=source, staging=staging,
                     params=params, symbols=symbols)

    return {
        f"{prefix}.meta_sync_all": meta_sync_all,
        f"{prefix}.meta_sync_symbols": meta_sync_symbols,
    }
