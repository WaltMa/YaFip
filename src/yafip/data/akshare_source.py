"""akshare 适配器：港股（HK）与美股（US）。

摸底结论（akshare 1.20.64 实测）
- 个股日 K 走 Sina（东方财富接口实测触发 IP 限流，不做主路径）
  - HK：ak.stock_hk_hist_sina(symbol='00700', period='daily', adjust='')
      返回 date/open/high/low/close/volume/amount（volume 股、amount 港元）
  - US：ak.stock_us_hist_sina(symbol='AAPL', period='daily', adjust='')
      返回 date/open/high/low/close/volume（无 amount；volume 股）
- 指数日 K：HK ak.index_hk_daily_sina / US ak.index_us_daily_sina（仅 .IXIC/.DJI/.INX/.NDX，无 VIX）
- 元数据：HK ak.stock_hk_spot_sina()（代码/中文名称/英文名称，分页慢）；US ak.stock_us_name_sina()（name/cname/symbol，轻量）
- 交易日历：akshare 无 HK/US 官方日历 → 用指数日 K 开市日推导（历史日，未来日需数据源补齐，已知边界）
- 复权：adj_type=NONE/1.0（不实现折算）；get_adj_factor 返回空，fetcher 对 NONE 市场写占位 1.0
- 市值：无 HK/US 市值接口 → instrument_metric 不写；白名单规则对 HK/US 不可用（手工维护）
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import pandas as pd

from yafip.data.base import (
    BASIC_COLUMNS,
    CALENDAR_COLUMNS,
    DAILY_COLUMNS,
    MV_COLUMNS,
    DataSource,
    DataSourceError,
    ensure_columns,
    normalize_date,
)
from yafip.data.market import MARKET_CN_A, MARKET_HK, MARKET_US

logger = logging.getLogger("yafip")

# US 日历骨架：新浪仅支持 4 大指数；VIX 无接口本期不注册
US_CAL_INDEX = ".INX"
# HK 日历骨架：中华港股通精选100 指数（已实测存在于 index_hk_spot_sina）
HK_CAL_INDEX = "CES100"


def _import_ak():
    try:
        import akshare as ak
    except ImportError as exc:
        raise DataSourceError("akshare 未安装：请 `uv pip install akshare` 后重试") from exc
    return ak


class AkshareSource(DataSource):
    name = "akshare"

    # -- 标的列表（元数据） -------------------------------------------------
    def get_stock_list(self, market: str) -> pd.DataFrame:
        ak = _import_ak()
        if market == MARKET_HK:
            df = ak.stock_hk_spot_sina()
            temp = df.rename(columns={"代码": "ts_code", "中文名称": "name"})
            temp["symbol"] = temp["ts_code"]
            temp["exchange"] = "SEHK"
            temp["currency"] = "HKD"
            temp["list_status"] = "L"
            # 保留 exchange/currency 等扩展列（BASIC_COLUMNS 之外的列）
            for column in BASIC_COLUMNS:
                if column not in temp.columns:
                    temp[column] = None
            return temp
        if market == MARKET_US:
            df = ak.stock_us_name_sina()
            temp = df.rename(columns={"symbol": "ts_code"})
            temp["name"] = temp["cname"]  # 中文名优先（避免 rename 撞列）
            temp["symbol"] = temp["ts_code"]
            temp["exchange"] = None
            temp["currency"] = "USD"
            temp["list_status"] = "L"
            for column in BASIC_COLUMNS:
                if column not in temp.columns:
                    temp[column] = None
            return temp
        raise DataSourceError(f"akshare 适配器仅支持 {MARKET_HK}/{MARKET_US}，收到：{market}（A 股请用 tushare）")

    # -- 交易日历（指数日 K 开市日推导） ------------------------------------
    def get_calendar(self, market: str, start: str, end: str) -> pd.DataFrame:
        ak = _import_ak()
        try:
            if market == MARKET_US:
                df = ak.index_us_daily_sina(symbol=US_CAL_INDEX)
            elif market == MARKET_HK:
                df = ak.index_hk_daily_sina(symbol=HK_CAL_INDEX)
            else:
                raise DataSourceError(f"akshare 适配器仅支持 {MARKET_HK}/{MARKET_US}，收到：{market}")
        except DataSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataSourceError(f"akshare 日历获取失败（{market}）：{exc}") from exc
        if df is None or df.empty or "date" not in df.columns:
            return pd.DataFrame(columns=CALENDAR_COLUMNS)
        result = df.copy()
        # 过滤非交易日 NaN 占位行（close 为空）
        if "close" in result.columns:
            result = result[result["close"].notna()]
        result["cal_date"] = result["date"].map(normalize_date)
        result = result[(result["cal_date"] >= start) & (result["cal_date"] <= end)]
        result["is_open"] = 1
        result["pretrade_date"] = None
        result["exchange"] = market
        return ensure_columns(result, CALENDAR_COLUMNS)

    # -- 日 K ---------------------------------------------------------------
    def get_daily(
        self,
        market: str,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        if market not in (MARKET_HK, MARKET_US):
            raise DataSourceError(f"akshare 适配器仅支持 {MARKET_HK}/{MARKET_US}，收到：{market}")
        codes = list(symbols or [])
        if not codes:
            raise DataSourceError("akshare 日K需要显式 symbols（该源无按交易日全市场接口）")
        df = self._daily_for_symbols(market, codes, trade_date, trade_date)
        return df

    def _daily_for_symbols(self, market: str, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        """按 symbol 逐个拉取日 K 并合并（akshare 无按日的批量接口）。

        单标的失败只跳过该标的（退市/改代码不阻断整日，避免 HK/US 增量卡死在同一水位）；
        失败清单经 `df.attrs["failed_symbols"]` 随结果传出（DataFrame.attrs 无共享状态、
        并发安全），供 fetcher 写入任务结果——否则失败标的缺失会静默永久化。
        全部失败时返回空表（也带 attrs），由 fetcher 按“空结果”判当日失败。
        """
        ak = _import_ak()
        frames: list[pd.DataFrame] = []
        failed_symbols: list[dict] = []
        for symbol in symbols:
            try:
                if market == MARKET_HK:
                    code = str(symbol).replace(".HK", "")
                    raw = ak.stock_hk_hist_sina(symbol=code, period="daily", adjust="")
                else:  # US
                    raw = ak.stock_us_hist_sina(symbol=str(symbol), period="daily", adjust="")
            except Exception as exc:  # noqa: BLE001 - 单标的失败不阻断
                failed_symbols.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if raw is None or raw.empty:
                failed_symbols.append({"symbol": symbol, "error": "返回空"})
                continue
            temp = raw.copy()
            temp["ts_code"] = symbol  # 源侧契约：日K以 ts_code 标识标的
            temp["trade_date"] = temp["date"].map(normalize_date)
            temp["vol"] = temp["volume"]  # akshare 列名为 volume（股）
            if "amount" not in temp.columns:
                temp["amount"] = None
            # 过滤非交易日的 NaN 占位行（新浪美股数据含非交易日期，close 为空）
            temp = temp[temp["close"].notna()].copy()
            # pre_close 用前一日收盘（Sina 无该列；历史序列完整，跨区间边界也正确）
            temp = temp.sort_values("trade_date")
            temp["pre_close"] = temp["close"].shift(1)
            temp = temp[(temp["trade_date"] >= start) & (temp["trade_date"] <= end)]
            frames.append(temp[["ts_code", "trade_date", "open", "high", "low", "close",
                                "pre_close", "vol", "amount"]])
        if failed_symbols:
            logger.warning(
                "akshare 日K %s 个标的失败（已跳过，不中断整日）：%s",
                len(failed_symbols),
                "; ".join(f"{f['symbol']}（{f['error']}）" for f in failed_symbols)[:500],
            )
        if not frames:
            empty = pd.DataFrame(columns=DAILY_COLUMNS)
            empty.attrs["failed_symbols"] = failed_symbols
            return empty
        result = pd.concat(frames, ignore_index=True)
        result.attrs["failed_symbols"] = failed_symbols
        return result

    # -- 复权因子（HK/US：无，NONE/1.0 由 fetcher 写占位） ------------------
    def get_adj_factor(self, market: str, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

    # -- 市值（HK/US：无接口，返回空） --------------------------------------
    def get_daily_basic(self, market: str, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(columns=MV_COLUMNS)

    # -- 连接测试 -----------------------------------------------------------
    def test_connection(self) -> dict:
        ak = _import_ak()
        steps: list[dict] = []
        try:
            df = ak.stock_us_name_sina()
            steps.append({"name": "stock_us_name", "ok": len(df) > 0, "message": f"美股标的 {len(df)} 条"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": "stock_us_name", "ok": False, "message": str(exc)[:120]})
        try:
            df = ak.index_us_daily_sina(symbol=US_CAL_INDEX)
            steps.append({"name": "index_us_daily", "ok": df is not None and not df.empty, "message": ".INX 日K可用"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": "index_us_daily", "ok": False, "message": str(exc)[:120]})
        return {"ok": all(s["ok"] for s in steps), "steps": steps}
