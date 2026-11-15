from __future__ import annotations

from conftest import FakeContext, FakeSource, make_db, seed_calendar

from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.data.market import MARKET_CN_A, MARKET_HK, MARKET_US
from yafip.data.repository import Repository
from yafip.data.staging import StagingStore
from yafip.data.tasks import resolve_adj_policy
from yafip.data.tasks.handlers import build_task_handlers
from yafip.data.whitelist import WhitelistService
from yafip.data.fetcher import Fetcher

MARKET = MARKET_CN_A


def make_handlers(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    audit = AuditService(db)
    source = FakeSource()
    whitelist = WhitelistService(db, repo, config, audit, source)
    seed_calendar(db, MARKET)
    staging = StagingStore(tmp_path, db, repo)
    fetcher = Fetcher(db, repo, config, source, whitelist, staging)
    handlers = build_task_handlers(
        config=config, repo=repo, source=source, staging=staging,
        fetcher=fetcher, whitelist=whitelist,
    )
    return db, repo, handlers


def test_resolve_adj_policy():
    assert resolve_adj_policy(MARKET_CN_A) == ("FACTOR", None)
    assert resolve_adj_policy(MARKET_HK) == ("NONE", 1.0)
    assert resolve_adj_policy(MARKET_US) == ("NONE", 1.0)


def test_meta_sync_all_writes_instrument_and_metric(tmp_path):
    db, repo, handlers = make_handlers(tmp_path)
    result = handlers["tasks.meta_sync_all"](FakeContext("meta-1"), {"market": MARKET})
    assert result["instrument_rows"] == 3
    assert result["metric_rows"] == 3
    assert repo.instrument_count(MARKET) == 3
    metric = db.query_one(
        "SELECT * FROM instrument_metric WHERE market=? AND symbol=?", (MARKET, "600519.SH")
    )
    assert metric["metric_key"] == "total_mv"
    assert metric["unit"] == "万元"
    assert metric["metric_value"] == 800000.0


def test_meta_sync_symbols_filters(tmp_path):
    db, repo, handlers = make_handlers(tmp_path)
    result = handlers["tasks.meta_sync_symbols"](
        FakeContext("meta-2"), {"market": MARKET, "symbols": ["600519.SH"]}
    )
    assert result["instrument_rows"] == 1
    assert result["metric_rows"] == 1
    assert repo.instrument_count(MARKET) == 1
    assert repo.get_instrument(MARKET, "600519.SH") is not None


def test_meta_classifies_asset_types(tmp_path):
    """asset_type 分类：源不带类型列时按 A 股代码规则推断（STOCK/ETF/INDEX）。"""
    from conftest import FakeSource

    class ClassifySource(FakeSource):
        STOCKS = ["600519.SH", "513100.SH", "000688.SH"]  # 股票 / 纳指ETF / 科创50指数

    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    audit = AuditService(db)
    source = ClassifySource()
    whitelist = WhitelistService(db, repo, config, audit, source)
    seed_calendar(db, MARKET)
    staging = StagingStore(tmp_path, db, repo)
    handlers = build_task_handlers(
        config=config, repo=repo, source=source, staging=staging,
        fetcher=None, whitelist=whitelist,
    )
    result = handlers["tasks.meta_sync_all"](FakeContext("meta-cls"), {"market": MARKET})
    assert result["instrument_rows"] == 3
    types = {
        repo.get_instrument(MARKET, s)["asset_type"]
        for s in ("600519.SH", "513100.SH", "000688.SH")
    }
    assert types == {"STOCK", "ETF", "INDEX"}


def test_bars_range_handler_does_not_touch_watermark(tmp_path):
    db, repo, handlers = make_handlers(tmp_path)
    # 白名单加成员（A 股条码可被 FakeSource 覆盖）
    from yafip.data.whitelist import WhitelistService

    config = ConfigService(db)
    whitelist = WhitelistService(db, repo, config, AuditService(db), FakeSource())
    whitelist.add_member(MARKET, "600519.SH")

    result = handlers["tasks.bars_range"](
        FakeContext("br-1"),
        {"market": MARKET, "start": "2025-01-02", "end": "2025-01-03"},
    )
    assert result["dates_done"] == 2
    assert repo.bar_count(MARKET) == 2
    assert repo.sync_state_watermark(MARKET) is None  # range 永不推进水位


def test_whitelist_handler_delegates(tmp_path):
    db, repo, handlers = make_handlers(tmp_path)
    # ：先落市值指标（读表规则），再触发白名单更新
    for symbol, value in {
        "600519.SH": 800000.0, "000001.SZ": 600000.0, "000002.SZ": 400000.0,
    }.items():
        repo.upsert_instrument(MARKET, [{"symbol": symbol, "name": symbol, "asset_type": "STOCK", "status": "L"}], source="tushare")
        repo.upsert_instrument_metric(MARKET, symbol, "total_mv", value, unit="万元", as_of_date="2025-01-06", source="tushare")

    result = handlers["tasks.whitelist_update"](
        FakeContext("wl-1"),
        {"market": MARKET, "metric": "total_mv", "op": ">", "value": 500000},
    )
    assert result["hit_count"] == 2  # 600519 / 000001
    assert repo.member_count(MARKET) == 2
