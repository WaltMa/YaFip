from __future__ import annotations

import sys

import pandas as pd
import pytest

from yafip.data.akshare_source import AkshareSource
from yafip.data.market import MARKET_HK, MARKET_US
from yafip.data.tasks.policy import market_units


class FakeAk:
    """模拟 akshare 接口（字段与实测一致）。"""

    def stock_hk_spot_sina(self):
        return pd.DataFrame([
            {"代码": "00700", "中文名称": "腾讯控股", "英文名称": "Tencent"},
        ])

    def stock_us_name_sina(self):
        return pd.DataFrame([
            {"name": "Apple", "cname": "苹果", "symbol": "AAPL"},
        ])

    def index_us_daily_sina(self, symbol):
        return pd.DataFrame([
            {"date": "2026-11-12", "open": 7000.0, "high": 7100.0, "low": 6950.0, "close": 7050.0, "volume": 1, "amount": 0},
            {"date": "2026-11-13", "open": 7050.0, "high": 7100.0, "low": 7000.0, "close": 7080.0, "volume": 1, "amount": 0},
        ])

    def index_hk_daily_sina(self, symbol):
        return pd.DataFrame([
            {"date": "2026-11-12", "open": 20000.0, "high": 20100.0, "low": 19900.0, "close": 20050.0, "volume": 1, "amount": 1},
        ])

    def stock_hk_hist_sina(self, symbol, period="daily", adjust=""):
        return pd.DataFrame([
            {"date": "2026-11-12", "open": 390.0, "high": 392.0, "low": 388.0, "close": 390.5, "volume": 1000000, "amount": 400000000},
            {"date": "2026-11-13", "open": 391.0, "high": 393.0, "low": 389.0, "close": 391.5, "volume": 1100000, "amount": 410000000},
        ])

    def stock_us_hist_sina(self, symbol, period="daily", adjust=""):
        return pd.DataFrame([
            {"date": "2026-11-12", "open": 200.0, "high": 205.0, "low": 198.0, "close": 202.0, "volume": 40000000},
            {"date": "2026-11-13", "open": 202.0, "high": 206.0, "low": 200.0, "close": 204.0, "volume": 41000000},
        ])


@pytest.fixture
def fake_ak(monkeypatch):
    sys.modules["akshare"] = FakeAk()
    yield
    del sys.modules["akshare"]


def test_hk_stock_list_maps_columns(fake_ak):
    source = AkshareSource()
    df = source.get_stock_list(MARKET_HK)
    assert "ts_code" in df.columns and "name" in df.columns
    assert df.iloc[0]["ts_code"] == "00700"
    assert df.iloc[0]["name"] == "腾讯控股"
    assert df.iloc[0]["currency"] == "HKD"


def test_us_stock_list_maps_columns(fake_ak):
    source = AkshareSource()
    df = source.get_stock_list(MARKET_US)
    assert df.iloc[0]["ts_code"] == "AAPL"
    assert df.iloc[0]["name"] == "苹果"
    assert df.iloc[0]["currency"] == "USD"


def test_hk_daily_per_symbol(fake_ak):
    source = AkshareSource()
    df = source.get_daily(MARKET_HK, "2026-11-13", symbols=["00700.HK"])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["ts_code"] == "00700.HK"
    assert row["trade_date"] == "2026-11-13"
    assert row["close"] == 391.5
    assert row["pre_close"] is None


def test_us_daily_no_amount(fake_ak):
    source = AkshareSource()
    df = source.get_daily(MARKET_US, "2026-11-13", symbols=["AAPL"])
    assert len(df) == 1
    assert df.iloc[0]["ts_code"] == "AAPL"
    assert df.iloc[0]["amount"] is None


def test_daily_requires_symbols(fake_ak):
    source = AkshareSource()
    with pytest.raises(Exception, match="symbols"):
        source.get_daily(MARKET_US, "2026-11-13")


def test_calendar_derived_from_index(fake_ak):
    source = AkshareSource()
    df = source.get_calendar(MARKET_US, "2026-11-01", "2026-11-30")
    assert len(df) == 2  # 仅 .INX 有数据的两个日期
    assert set(df["cal_date"]) == {"2026-11-12", "2026-11-13"}
    assert int(df.iloc[0]["is_open"]) == 1

    hk = source.get_calendar(MARKET_HK, "2026-11-01", "2026-11-30")
    assert len(hk) == 1


def test_daily_basic_empty_for_hk_us(fake_ak):
    source = AkshareSource()
    df = source.get_daily_basic(MARKET_US, "2026-11-13")
    assert df.empty


def test_adj_factor_empty_for_hk_us(fake_ak):
    source = AkshareSource()
    df = source.get_adj_factor(MARKET_HK, "2026-11-13")
    assert df.empty


def test_market_units():
    assert market_units("CN_A") == ("CNY", "手", "千元")
    assert market_units(MARKET_HK) == ("HKD", "股", "港元")
    assert market_units(MARKET_US) == ("USD", "股", "美元")
