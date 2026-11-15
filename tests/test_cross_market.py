from __future__ import annotations

import pandas as pd
import pytest

from conftest import FakeContext, FakeSource, make_db, seed_calendar

from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.data.market import MARKET_CN_A, MARKET_HK, MARKET_US
from yafip.data.repository import Repository
from yafip.data.tasks import resolve_adj_policy
from yafip.data.whitelist import WhitelistService


def test_symbol_pk_is_per_market(tmp_path):
    """同一 symbol 在不同市场是不同标的（主键 (market, symbol)）。"""
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        repo.upsert_instrument(MARKET_CN_A, [{"symbol": "00700", "name": "腾讯A?", "asset_type": "STOCK", "status": "L"}], source="tushare")
        repo.upsert_instrument(MARKET_HK, [{"symbol": "00700.HK", "name": "腾讯控股", "asset_type": "STOCK", "status": "L"}], source="akshare")
        assert repo.get_instrument(MARKET_CN_A, "00700") is not None
        assert repo.get_instrument(MARKET_HK, "00700.HK") is not None
        assert repo.instrument_count(MARKET_CN_A) == 1
        assert repo.instrument_count(MARKET_HK) == 1
        assert repo.instrument_count() == 2
    finally:
        db.close_all()


def test_calendar_is_per_market(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        seed_calendar(db, MARKET_CN_A, ["2025-01-02", "2025-01-03"])
        seed_calendar(db, MARKET_HK, ["2025-01-02"])
        assert repo.open_dates(MARKET_CN_A, "2025-01-01", "2025-01-31") == ["2025-01-02", "2025-01-03"]
        assert repo.open_dates(MARKET_HK, "2025-01-01", "2025-01-31") == ["2025-01-02"]
        assert repo.open_dates(MARKET_US, "2025-01-01", "2025-01-31") == []
    finally:
        db.close_all()


def test_adj_policy_hk_us_none(tmp_path):
    assert resolve_adj_policy(MARKET_CN_A) == ("FACTOR", None)
    assert resolve_adj_policy(MARKET_HK) == ("NONE", 1.0)
    assert resolve_adj_policy(MARKET_US) == ("NONE", 1.0)


def test_bars_isolated_per_market(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        df_cn = pd.DataFrame(
            [{"symbol": "600519.SH", "trade_date": "2026-11-13", "open": 1500.0, "high": 1520.0,
              "low": 1490.0, "close": 1510.0, "pre_close": 1500.0, "vol": 100.0, "amount": 200.0}]
        )
        df_hk = pd.DataFrame(
            [{"symbol": "00700.HK", "trade_date": "2026-11-13", "open": 500.0, "high": 510.0,
              "low": 495.0, "close": 505.0, "pre_close": 500.0, "vol": 1000.0, "amount": 500.0}]
        )
        repo.upsert_bars(MARKET_CN_A, df_cn, source="tushare", currency="CNY", vol_unit="手", amount_unit="千元")
        repo.upsert_bars(MARKET_HK, df_hk, source="akshare", currency="HKD", vol_unit="股", amount_unit="港元")
        assert repo.bar_count(MARKET_CN_A) == 1
        assert repo.bar_count(MARKET_HK) == 1
        assert repo.bar_count() == 2
        rows = repo.daily_query(MARKET_HK, "00700.HK", "2026-11-01", "2026-11-30")
        assert rows[0]["close"] == 505.0
        assert repo.daily_query(MARKET_CN_A, "00700.HK", "2026-11-01", "2026-11-30") == []
    finally:
        db.close_all()


def test_whitelist_is_per_market(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    whitelist = WhitelistService(db, repo, ConfigService(db), AuditService(db), FakeSource())
    try:
        whitelist.add_member(MARKET_CN_A, "600519.SH")
        whitelist.add_member(MARKET_HK, "00700.HK")
        whitelist.add_member(MARKET_US, "AAPL")
        assert whitelist.state(MARKET_CN_A) == {"600519.SH"}
        assert whitelist.state(MARKET_HK) == {"00700.HK"}
        assert whitelist.state(MARKET_US) == {"AAPL"}
    finally:
        db.close_all()


def test_index_and_vix_have_no_metric_rows(tmp_path):
    """指数/VIX 无市值：instrument_metric 不写行（不强制打平）。"""
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        repo.upsert_instrument(MARKET_CN_A, [{"symbol": "000688.SH", "name": "科创50", "asset_type": "INDEX", "status": "L"}], source="tushare")
        repo.upsert_instrument(MARKET_US, [{"symbol": "^VIX", "name": "VIX", "asset_type": "VIX", "status": "L"}], source="akshare")
        # 无市值 → 无 metric 行
        assert db.scalar("SELECT COUNT(*) FROM instrument_metric") == 0
        assert repo.get_instrument(MARKET_US, "^VIX")["asset_type"] == "VIX"
    finally:
        db.close_all()


def test_hk_us_adj_none_keeps_query_rows(tmp_path):
    """HK/US 无因子：adj_type=NONE，qfq/hfq 查询 LEFT JOIN 兜底不丢行。"""
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        df = pd.DataFrame(
            [{"symbol": "AAPL", "trade_date": "2026-11-13", "open": 200.0, "high": 205.0,
              "low": 198.0, "close": 202.0, "pre_close": 199.0, "vol": 1000.0, "amount": 200.0}]
        )
        repo.upsert_bars(MARKET_US, df, source="akshare", currency="USD", vol_unit="股", amount_unit="美元")
        # 无因子行：raw 与 qfq/hfq 均不丢行
        for price_type in ("raw", "qfq", "hfq"):
            rows = repo.daily_query(MARKET_US, "AAPL", "2026-11-01", "2026-11-30", price_type=price_type)
            assert len(rows) == 1, price_type
            assert rows[0]["close"] == 202.0, price_type
    finally:
        db.close_all()
