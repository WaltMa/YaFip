from __future__ import annotations

from datetime import date

import pandas as pd

from yafip.config import ConfigService
from yafip.core.db import Database, now_iso
from yafip.data.base import (
    ADJ_COLUMNS,
    BASIC_COLUMNS,
    CALENDAR_COLUMNS,
    DAILY_COLUMNS,
    MV_COLUMNS,
    DataSource,
)


class FakeContext:
    def __init__(self, task_id: str = "test-task"):
        self.task_id = task_id
        self.cancelled = False
        self.progress_log: list[tuple] = []
        self.logs: list[tuple] = []

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled

    def checkpoint(self) -> None:
        return None

    def progress(self, percent, stage=None, message=None) -> None:
        self.progress_log.append((percent, stage, message))

    def log(self, level, message) -> None:
        self.logs.append((level, message))


class FakeSource(DataSource):
    """3 个交易日、3 只股票；最后一天因子翻倍，用于验证前复权锚点。"""

    name = "fake"
    OPEN_DATES = ["2025-01-02", "2025-01-03", "2025-01-06"]
    STOCKS = ["000001.SZ", "000002.SZ", "600519.SH"]

    def get_calendar(self, market: str, start: str, end: str) -> pd.DataFrame:
        rows = []
        for day in self.OPEN_DATES:
            if start <= day <= end:
                rows.append({"exchange": "SSE", "cal_date": day, "is_open": 1, "pretrade_date": None})
        return pd.DataFrame(rows, columns=CALENDAR_COLUMNS)

    def get_stock_list(self, market: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "symbol": code.split(".")[0],
                    "name": "股票" + str(index),
                    "area": "深圳",
                    "industry": "测试",
                    "market": "主板",
                    "list_date": "2020-01-01",
                    "delist_date": None,
                    "list_status": "L",
                    "is_north": 0,
                }
                for index, code in enumerate(self.STOCKS)
            ],
            columns=BASIC_COLUMNS,
        )

    def get_daily(
        self,
        market: str,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        if trade_date not in self.OPEN_DATES:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        index = self.OPEN_DATES.index(trade_date)
        return pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "trade_date": trade_date,
                    "open": 10.0 + index,
                    "high": 11.0 + index,
                    "low": 9.0 + index,
                    "close": 10.5 + index,
                    "pre_close": 10.0 + index,
                    "change": 0.5,
                    "pct_chg": 5.0,
                    "vol": 1000.0,
                    "amount": 10500.0,
                }
                for code in self.STOCKS
            ],
            columns=DAILY_COLUMNS,
        )

    def get_adj_factor(self, market: str, trade_date: str) -> pd.DataFrame:
        if trade_date not in self.OPEN_DATES:
            return pd.DataFrame(columns=ADJ_COLUMNS)
        factor = 2.0 if trade_date == self.OPEN_DATES[-1] else 1.0
        return pd.DataFrame(
            [{"ts_code": code, "trade_date": trade_date, "adj_factor": factor} for code in self.STOCKS],
            columns=ADJ_COLUMNS,
        )

    def get_daily_basic(self, market: str, trade_date: str) -> pd.DataFrame:
        if trade_date not in self.OPEN_DATES:
            return pd.DataFrame(columns=MV_COLUMNS)
        values = {"000001.SZ": 600000.0, "000002.SZ": 400000.0, "600519.SH": 800000.0}
        return pd.DataFrame(
            [
                {"ts_code": code, "trade_date": trade_date, "total_mv": value}
                for code, value in values.items()
            ],
            columns=MV_COLUMNS,
        )

    def test_connection(self) -> dict:
        return {"ok": True, "steps": []}


def make_db(tmp_path) -> Database:
    db = Database(tmp_path / "yafip_test.db")
    db.init_schema()  # 含版本守卫 + 迁移脚手架
    config = ConfigService(db)
    config.seed_defaults()
    return db


def seed_calendar(db: Database, market: str = "CN_A", days: list[str] | None = None) -> None:
    """按市场写入 FakeSource 的交易日历。"""
    for day in days or FakeSource.OPEN_DATES:
        db.execute(
            "INSERT OR REPLACE INTO trade_calendar(market, cal_date, is_open, pretrade_date) "
            "VALUES(?, ?, 1, NULL)",
            (market, day),
        )
