from __future__ import annotations

import pandas as pd

from conftest import FakeSource, make_db, seed_calendar

from yafip.data.market import MARKET_CN_A, MARKET_HK
from yafip.data.repository import Repository


def test_instrument_upsert_and_query(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        records = [
            {"symbol": "600519.SH", "name": "贵州茅台", "asset_type": "STOCK", "currency": "CNY",
             "list_date": "2001-08-27", "status": "L", "ext_json": '{"industry":"白酒"}'},
            {"symbol": "513100.SH", "name": "纳指ETF", "asset_type": "ETF", "currency": "CNY",
             "list_date": "2013-04-25", "status": "L"},
            {"symbol": "000688.SH", "name": "科创50", "asset_type": "INDEX", "currency": "CNY",
             "status": "L"},
        ]
        assert repo.upsert_instrument(MARKET_CN_A, records, source="tushare") == 3

        row = repo.get_instrument(MARKET_CN_A, "600519.SH")
        assert row["name"] == "贵州茅台"
        assert row["asset_type"] == "STOCK"
        assert row["source"] == "tushare"
        assert repo.instrument_count() == 3
        assert repo.instrument_count(MARKET_CN_A) == 3

        # 更新同名标的：upsert 覆盖
        repo.upsert_instrument(
            MARKET_CN_A,
            [{"symbol": "600519.SH", "name": "贵州茅台（改）", "asset_type": "STOCK", "status": "L"}],
            source="tushare",
        )
        assert repo.instrument_count() == 3
        assert repo.get_instrument(MARKET_CN_A, "600519.SH")["name"] == "贵州茅台（改）"

        # 过滤
        items, total = repo.instruments(market=MARKET_CN_A, asset_type="ETF")
        assert total == 1 and items[0]["symbol"] == "513100.SH"
        items, total = repo.instruments(keyword="茅台")
        assert total == 1
    finally:
        db.close_all()


def test_instrument_metric_upsert(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        repo.upsert_instrument_metric(MARKET_CN_A, "600519.SH", "total_mv", 2000000.0,
                                      unit="万元", as_of_date="2026-11-13", source="tushare")
        repo.upsert_instrument_metric(MARKET_CN_A, "600519.SH", "total_mv", 2100000.0,
                                      unit="万元", as_of_date="2026-11-14", source="tushare")
        rows = db.query("SELECT * FROM instrument_metric")
        assert len(rows) == 1
        assert rows[0]["metric_value"] == 2100000.0
        assert rows[0]["as_of_date"] == "2026-11-14"
    finally:
        db.close_all()


def test_bars_upsert_idempotent_with_source(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        df = pd.DataFrame(
            [
                {"symbol": "600519.SH", "trade_date": "2026-11-13", "open": 1500.0, "high": 1520.0,
                 "low": 1490.0, "close": 1510.0, "pre_close": 1500.0, "vol": 30000.0, "amount": 45000.0},
            ]
        )
        assert repo.upsert_bars(MARKET_CN_A, df, source="tushare", currency="CNY",
                                vol_unit="手", amount_unit="千元") == 1
        # 幂等重写 + 换源覆盖
        df2 = df.copy()
        df2["close"] = 1520.0
        assert repo.upsert_bars(MARKET_CN_A, df2, source="akshare", currency="CNY",
                                vol_unit="手", amount_unit="千元") == 1
        rows = db.query("SELECT * FROM bar_daily")
        assert len(rows) == 1
        assert rows[0]["close"] == 1520.0
        assert rows[0]["source"] == "akshare"
        assert rows[0]["vol_unit"] == "手"
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
        assert repo.latest_open_date(MARKET_CN_A, "2025-01-31") == "2025-01-03"
        assert repo.is_open(MARKET_HK, "2025-01-02") == 1
    finally:
        db.close_all()


def test_sync_state_watermark(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        assert repo.sync_state_watermark(MARKET_CN_A) is None
        repo.set_sync_state(MARKET_CN_A, "2026-11-13", status="ok")
        assert repo.sync_state_watermark(MARKET_CN_A) == "2026-11-13"
        # 覆盖推进
        repo.set_sync_state(MARKET_CN_A, "2026-11-14", status="ok")
        assert repo.sync_state_watermark(MARKET_CN_A) == "2026-11-14"
        # 市场隔离
        assert repo.sync_state_watermark(MARKET_HK) is None
        state = repo.get_sync_state(MARKET_CN_A)
        assert state["dataset"] == "bars" and state["scope"] == "WHITELIST"
    finally:
        db.close_all()


def test_whitelist_upsert_only_and_per_market(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        repo.upsert_member(MARKET_CN_A, "600519.SH", rule_key="total_mv_threshold",
                           metric_value=800000.0, as_of_date="2026-11-13", source="rule")
        # 只更新指标，不产生第二行、不删除
        repo.upsert_member(MARKET_CN_A, "600519.SH", rule_key="total_mv_threshold",
                           metric_value=900000.0, as_of_date="2026-11-14", source="rule")
        assert repo.member_count() == 1
        row = repo.get_member(MARKET_CN_A, "600519.SH")
        assert row["metric_value"] == 900000.0

        repo.upsert_member(MARKET_HK, "00700.HK", source="manual", reason="手动")
        assert repo.member_count() == 2
        assert repo.member_count(MARKET_CN_A) == 1
        assert repo.whitelist_codes(MARKET_CN_A) == ["600519.SH"]
        assert repo.whitelist_codes(MARKET_HK) == ["00700.HK"]

        assert repo.delete_member(MARKET_CN_A, "600519.SH") == 1
        assert repo.member_count(MARKET_CN_A) == 0
    finally:
        db.close_all()


def test_quality_issue_market_natural_key(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        issue = {"check_type": "adj_missing", "market": MARKET_CN_A, "symbol": "600519.SH",
                 "trade_date": "2026-11-13", "detail": "x"}
        repo.insert_issues([issue])
        # 同一自然键 upsert（detail 覆盖），不重复
        repo.insert_issues([{**issue, "detail": "y"}])
        rows = db.query("SELECT * FROM data_quality_issue")
        assert len(rows) == 1
        assert rows[0]["detail"] == "y"
        # 市场不同 → 不同自然键
        repo.insert_issues([{**issue, "market": MARKET_HK, "symbol": "00700.HK"}])
        assert repo.unresolved_issue_count() == 2
        assert repo.unresolved_issue_count(MARKET_CN_A) == 1
    finally:
        db.close_all()


def test_overview_stats_and_coverage(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        stats = repo.overview_stats()
        assert set(stats) == {"instruments", "bar_rows", "bar_dates", "adj_rows",
                              "latest_bar_date", "db_bytes", "quality_open", "whitelist"}
        assert stats["whitelist"]["members"] == 0
        # coverage 分组
        df = pd.DataFrame(
            [
                {"symbol": "600519.SH", "trade_date": f"2026-{m:02d}-01", "open": 1.0, "high": 2.0,
                 "low": 1.0, "close": 1.5, "pre_close": 1.0, "vol": 100.0, "amount": 200.0}
                for m in (1, 2)
            ]
        )
        repo.upsert_bars(MARKET_CN_A, df, source="fake")
        cov = repo.coverage(MARKET_CN_A)
        assert len(cov) == 1 and cov[0]["rows"] == 2
        assert repo.latest_bar_date(MARKET_CN_A) == "2026-02-01"
    finally:
        db.close_all()
