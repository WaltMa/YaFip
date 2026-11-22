"""数据源抽象与统一 Schema。

所有适配器返回统一列名、ISO 日期（YYYY-MM-DD）的 DataFrame。
单位：price 元、vol 手、amount 千元、total_mv 万元。
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

BASIC_COLUMNS = [
    "ts_code", "symbol", "name", "area", "industry", "market",
    "list_date", "delist_date", "list_status", "is_north",
]
CALENDAR_COLUMNS = ["exchange", "cal_date", "is_open", "pretrade_date"]
DAILY_COLUMNS = [
    "ts_code", "trade_date", "open", "high", "low", "close",
    "pre_close", "vol", "amount",
]
ADJ_COLUMNS = ["ts_code", "trade_date", "adj_factor"]
MV_COLUMNS = ["ts_code", "trade_date", "total_mv"]

# §4.2 预留：港美股复权本期只留逻辑，不实现具体数据源折算。
# 未来由 executor 的 normalize_adj() 钩子决定 adj_type，无因子市场写 NONE / 1.0。
ADJ_TYPES = ("FACTOR", "SPLIT", "NONE")


class DataSourceError(Exception):
    code = "1002"
    http_status = 502


class FatalDataSourceError(DataSourceError):
    code = "1003"
    http_status = 400


class TokenInvalid(FatalDataSourceError):
    pass


def normalize_date(value: Any) -> str | None:
    """tushare 的 20250103 / Timestamp / '2025-01-03' 统一为 ISO。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


def to_tushare_date(value: str) -> str:
    return str(value).replace("-", "")[:8]


def empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if df is None:
        return empty_frame(columns)
    if df.empty:
        return empty_frame(columns)
    for column in columns:
        if column not in df.columns:
            df[column] = None
    return df[columns]


class DataSource(ABC):
    name: str = "base"

    @abstractmethod
    def get_stock_list(self, market: str) -> pd.DataFrame: ...

    @abstractmethod
    def get_calendar(self, market: str, start: str, end: str) -> pd.DataFrame: ...

    @abstractmethod
    def get_daily(
        self,
        market: str,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame: ...

    @abstractmethod
    def get_adj_factor(self, market: str, trade_date: str) -> pd.DataFrame: ...

    @abstractmethod
    def get_daily_basic(self, market: str, trade_date: str) -> pd.DataFrame: ...

    @abstractmethod
    def test_connection(self) -> dict: ...
