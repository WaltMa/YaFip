from __future__ import annotations

import pandas as pd
import pytest

from conftest import FakeContext, FakeSource, make_db, seed_calendar

from yafip.config import ConfigService
from yafip.data.base import ADJ_COLUMNS, DAILY_COLUMNS
from yafip.data.fetcher import Fetcher
from yafip.data.market import MARKET_CN_A
from yafip.data.repository import Repository
from yafip.data.staging import StagingStore
from yafip.data.whitelist import WhitelistError, WhitelistService
from yafip.core.audit import AuditService

MARKET = MARKET_CN_A


def build(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    audit = AuditService(db)
    source = FakeSource()
    whitelist = WhitelistService(db, repo, config, audit, source)
    for code in FakeSource.STOCKS:
        whitelist.add_member(MARKET, code)
    seed_calendar(db, MARKET)
    staging = StagingStore(tmp_path, db, repo)
    fetcher = Fetcher(db, repo, config, source, whitelist, staging)
    return db, config, repo, whitelist, fetcher


def test_empty_whitelist_blocks_fetch(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    whitelist = WhitelistService(db, repo, config, AuditService(db), FakeSource())
    seed_calendar(db, MARKET)
    fetcher = Fetcher(db, repo, config, FakeSource(), whitelist, StagingStore(tmp_path, db, repo))
    with pytest.raises(WhitelistError):
        fetcher.run(
            FakeContext("task-empty"),
            {"market": MARKET, "mode": "range", "start": "2025-01-01", "end": "2025-01-06"},
        )
    assert repo.bar_count() == 0


def test_full_fetch_and_adjustment(tmp_path):
    db, config, repo, whitelist, fetcher = build(tmp_path)
    ctx = FakeContext("task-1")
    result = fetcher.run(
        ctx,
        {"market": MARKET, "mode": "full", "start": "2025-01-01", "end": "2025-01-06"},
    )

    assert result["dates_total"] == 3
    assert result["dates_done"] == 3
    assert result["dates_failed"] == 0
    assert repo.bar_count() == 9
    assert repo.adj_count() == 9
    # full 模式不动水位
    assert repo.sync_state_watermark(MARKET) is None

    # 前复权：第一天 close=10.5，因子 1.0 / 最新 2.0 => 5.25
    rows = repo.daily_query(MARKET, "000001.SZ", "2025-01-02", "2025-01-02", price_type="qfq")
    assert rows[0]["close"] == 5.25
    # 后复权：10.5 * 1.0
    hfq = repo.daily_query(MARKET, "000001.SZ", "2025-01-02", "2025-01-02", price_type="hfq")
    assert hfq[0]["close"] == 10.5

    # 无逐日断点：full 重跑即重抓（幂等 upsert，行数不变）
    again = fetcher.run(
        FakeContext("task-2"),
        {"market": MARKET, "mode": "full", "start": "2025-01-01", "end": "2025-01-06"},
    )
    assert again["dates_done"] == 3
    assert repo.bar_count() == 9


def test_incremental_watermark_semantics(tmp_path):
    db, config, repo, whitelist, fetcher = build(tmp_path)
    first = fetcher.run(FakeContext("task-a"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert first["dates_done"] == 3
    assert first["watermark_advanced"] is True
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"

    # 水位已到最新：无新交易日
    second = fetcher.run(FakeContext("task-b"), {"market": MARKET, "mode": "incremental", "end": "2025-01-10"})
    assert second["dates_total"] == 0
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"


def test_incremental_partial_failure_keeps_watermark(tmp_path, monkeypatch):
    db, config, repo, whitelist, fetcher = build(tmp_path)
    source = fetcher.source
    real_daily = source.get_daily

    def empty_for_0103(market, trade_date, symbols=None):
        if trade_date == "2025-01-03":
            return pd.DataFrame(columns=DAILY_COLUMNS)  # 模拟盘中未发布 / 积分不足
        return real_daily(market, trade_date, symbols=symbols)

    monkeypatch.setattr(source, "get_daily", empty_for_0103)
    first = fetcher.run(FakeContext("t1"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert first["dates_done"] == 2
    assert first["dates_failed"] == 1
    # 部分失败：水位不推进，下次从原水位重扫（失败日自然被重抓）
    assert repo.sync_state_watermark(MARKET) is None
    assert repo.bar_count() == 6

    monkeypatch.undo()
    second = fetcher.run(FakeContext("t2"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    # 重跑复用：01-02/01-06 已 applied 跳过，仅重抓失败日 01-03
    assert second["dates_total"] == 3
    assert second["dates_done"] == 1
    assert second["dates_reused"] == 2
    assert second["dates_failed"] == 0
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"
    assert repo.bar_count() == 9


def test_empty_adj_keeps_daily_but_failed(tmp_path, monkeypatch):
    db, config, repo, whitelist, fetcher = build(tmp_path)
    source = fetcher.source
    real_adj = source.get_adj_factor

    def empty_adj_for_0103(market, trade_date):
        if trade_date == "2025-01-03":
            return pd.DataFrame(columns=ADJ_COLUMNS)  # 因子晚于日 K 发布
        return real_adj(market, trade_date)

    monkeypatch.setattr(source, "get_adj_factor", empty_adj_for_0103)
    first = fetcher.run(FakeContext("t1"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    # daily 已入库但单元记失败（等待补齐因子）
    assert first["dates_failed"] == 1
    assert repo.bar_count() == 9
    assert repo.adj_count() == 6
    assert repo.sync_state_watermark(MARKET) is None

    monkeypatch.undo()
    second = fetcher.run(FakeContext("t2"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert second["dates_total"] == 3
    assert second["dates_done"] == 1   # 复用已 applied 的两天，仅重抓 01-03 补因子
    assert second["dates_reused"] == 2
    assert second["dates_failed"] == 0
    assert repo.adj_count() == 9
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"


def test_whitelist_change_does_not_reset_watermark(tmp_path):
    """G8：白名单变动不再使增量退化为全量重扫。"""
    db, config, repo, whitelist, fetcher = build(tmp_path)
    # 先删掉 600519，让首轮增量只同步 2 只
    whitelist.delete_member(MARKET, "600519.SH")
    first = fetcher.run(FakeContext("t1"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert first["dates_done"] == 3
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"
    assert repo.db.scalar("SELECT COUNT(*) FROM bar_daily WHERE symbol='600519.SH'") == 0

    # 新增白名单成员 → 水位不受影响；增量仍从 [水位+1, 最新] 规划
    whitelist.add_member(MARKET, "600519.SH")
    after = fetcher.run(FakeContext("t2"), {"market": MARKET, "mode": "incremental", "end": "2025-01-10"})
    assert after["dates_total"] == 0
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"

    # 新成员历史按  用指定同步（range）补
    backfill = fetcher.run(
        FakeContext("t3"),
        {"market": MARKET, "mode": "range", "start": "2025-01-01", "end": "2025-01-06", "symbols": ["600519.SH"]},
    )
    assert backfill["dates_done"] == 3
    assert repo.db.scalar("SELECT COUNT(*) FROM bar_daily WHERE symbol='600519.SH'") == 3
    # range 不动水位
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"


def test_range_requires_dates_and_ignores_watermark(tmp_path):
    db, config, repo, whitelist, fetcher = build(tmp_path)
    with pytest.raises(Exception):
        fetcher.run(FakeContext("t0"), {"market": MARKET, "mode": "range"})

    result = fetcher.run(
        FakeContext("t1"),
        {"market": MARKET, "mode": "range", "start": "2025-01-02", "end": "2025-01-03"},
    )
    assert result["dates_done"] == 2
    assert repo.sync_state_watermark(MARKET) is None  # 指定同步不涉及更新日期


def test_reuse_does_not_refetch_applied_days(tmp_path, monkeypatch):
    """验收 #3：重跑复用 staging 文件，数据源请求数不重复。"""
    db, config, repo, whitelist, fetcher = build(tmp_path)
    source = fetcher.source
    calls = {"daily": 0, "adj": 0}
    real_daily, real_adj = source.get_daily, source.get_adj_factor

    def counted_daily(market, trade_date, symbols=None):
        calls["daily"] += 1
        return real_daily(market, trade_date, symbols=symbols)

    def counted_adj(market, trade_date):
        calls["adj"] += 1
        return real_adj(market, trade_date)

    monkeypatch.setattr(source, "get_daily", counted_daily)
    monkeypatch.setattr(source, "get_adj_factor", counted_adj)

    first = fetcher.run(FakeContext("t1"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert first["dates_done"] == 3
    assert calls["daily"] == 3
    assert calls["adj"] == 3

    # 模拟水位回退（如上次部分失败未推进）：整窗口重扫，全部复用
    repo.set_sync_state(MARKET, None)
    second = fetcher.run(FakeContext("t2"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert second["dates_reused"] == 3
    assert second["dates_done"] == 0
    assert calls["daily"] == 3  # 请求数不增加
    assert calls["adj"] == 3
    assert repo.sync_state_watermark(MARKET) == "2025-01-06"


def test_killed_run_reuses_validated_files(tmp_path, monkeypatch):
    """kill 于 commit 前留下的 VALIDATED 文件，重跑复用（不重抓源、数据落库、水位推进）。"""
    db, config, repo, whitelist, fetcher = build(tmp_path)
    market = MARKET
    source = fetcher.source
    calls = {"daily": 0, "adj": 0}
    real_daily, real_adj = source.get_daily, source.get_adj_factor

    def counted_daily(m, trade_date, symbols=None):
        calls["daily"] += 1
        return real_daily(m, trade_date, symbols=symbols)

    def counted_adj(m, trade_date):
        calls["adj"] += 1
        return real_adj(m, trade_date)

    monkeypatch.setattr(source, "get_daily", counted_daily)
    monkeypatch.setattr(source, "get_adj_factor", counted_adj)

    # 模拟被 kill 的旧任务：2025-01-02 已写单元并通过校验（VALIDATED），未提交
    staging = fetcher.staging
    codes = whitelist.state(market)
    day = "2025-01-02"
    mapped, _ = fetcher._map_filter(real_daily(market, day, symbols=sorted(codes)), market, codes)
    bars_csv = staging.write_unit(
        batch_id="old-kill", task_id="t-old", task_type="bars.incremental",
        executor="fake.bars.incremental", source="fake", market=market,
        dataset="bars", unit_key=day, kind="bars", df=mapped, params_snapshot={},
    )
    assert staging.validate_unit(bars_csv, market=market, kind="bars") == []
    staging.mark_unit_status("bars", market, "old-kill", bars_csv.name, "validated")
    adj_mapped, _ = fetcher._map_filter(real_adj(market, day), market, codes)
    adj_csv = staging.write_unit(
        batch_id="old-kill", task_id="t-old", task_type="bars.incremental",
        executor="fake.bars.incremental", source="fake", market=market,
        dataset="bars", unit_key=day, kind="adj", df=adj_mapped, params_snapshot={},
    )
    staging.validate_unit(adj_csv, market=market, kind="adj")
    staging.mark_unit_status("bars", market, "old-kill", adj_csv.name, "validated")

    # 重跑增量：01-02 复用提交（不重抓），01-03/01-06 正常抓
    result = fetcher.run(FakeContext("t-retry"), {"market": market, "mode": "incremental", "end": "2025-01-06"})
    assert calls["daily"] == 2  # 仅 01-03/01-06 请求源
    assert calls["adj"] == 2
    assert result["dates_done"] == 2
    assert result["dates_reused"] == 0  # 复用提交不计入"跳过"
    assert repo.bar_count(market) == 9
    assert repo.db.scalar(
        "SELECT COUNT(*) FROM bar_daily WHERE market=? AND trade_date='2025-01-02'", (market,)
    ) == 3  # 复用日数据落库
    assert repo.sync_state_watermark(market) == "2025-01-06"
    assert result["watermark_advanced"] is True


def test_incremental_does_not_reuse_range_files(tmp_path):
    """range 遗留文件不被 incremental 误用（任务类型/执行器/标的集合隔离），无永久空洞。"""
    db, config, repo, whitelist, fetcher = build(tmp_path)
    market = MARKET
    # range 只显式拉 600519 两天（executor 与单元写入一致）
    range_result = fetcher.run(
        FakeContext("t-range"),
        {"market": market, "mode": "range", "start": "2025-01-02", "end": "2025-01-03",
         "symbols": ["600519.SH"], "executor": "fake.bars.range"},
    )
    assert range_result["dates_done"] == 2

    # incremental（白名单 3 只）→ 不复用 range 文件，窗口内全部重抓
    inc = fetcher.run(
        FakeContext("t-inc"),
        {"market": market, "mode": "incremental", "end": "2025-01-03", "executor": "fake.bars.incremental"},
    )
    assert inc["dates_reused"] == 0
    assert inc["dates_done"] == 3  # 窗口 = [2010-01-01, 最新开市日]（01-02/01-03/01-06）
    assert repo.bar_count(market) == 9  # 3 天 × 3 只（000001/000002 未因 range 文件被跳过而缺失）
    assert repo.sync_state_watermark(market) == "2025-01-06"


# ======================================================================
# 复权查询（沿袭 .. / A-10；market 维度）
# ======================================================================

def _seed_three_day_sample(repo) -> str:
    """构造 3 日样本：close=[10,10,20]、因子=[1,1,2]；返回 symbol。"""
    symbol = "000001.SZ"
    dates = ["2020-01-02", "2020-01-03", "2020-01-06"]
    closes = [10.0, 10.0, 20.0]
    pres = [9.0, 10.0, 10.0]
    changes = [1.0, 0.0, 10.0]
    pcts = [11.11, 0.0, 100.0]
    vols = [1000.0, 1000.0, 2000.0]
    amounts = [10000.0, 10000.0, 40000.0]
    daily = pd.DataFrame(
        [
            {
                "symbol": symbol, "trade_date": d, "open": 10.0, "high": 11.0, "low": 9.0,
                "close": c, "pre_close": pc, "change": ch, "pct_chg": pct,
                "vol": v, "amount": amt,
            }
            for d, c, pc, ch, pct, v, amt in zip(
                dates, closes, pres, changes, pcts, vols, amounts
            )
        ],
        columns=["symbol", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"],
    )
    adj = pd.DataFrame(
        [
            {"symbol": symbol, "trade_date": d, "adj_factor": f}
            for d, f in zip(dates, [1.0, 1.0, 2.0])
        ],
        columns=["symbol", "trade_date", "adj_factor"],
    )
    repo.upsert_bars(MARKET, daily, source="fake")
    repo.upsert_adj(MARKET, adj, source="fake")
    return symbol


def test_a04_price_adjustment_numeric_regression(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    code = _seed_three_day_sample(repo)
    try:
        raw = repo.daily_query(MARKET, code, "2020-01-01", "2020-01-31", price_type="raw")
        hfq = repo.daily_query(MARKET, code, "2020-01-01", "2020-01-31", price_type="hfq")
        qfq = repo.daily_query(MARKET, code, "2020-01-01", "2020-01-31", price_type="qfq")
        assert [r["close"] for r in raw] == [10.0, 10.0, 20.0]
        assert [r["close"] for r in hfq] == [10.0, 10.0, 40.0]
        assert [r["close"] for r in qfq] == [5.0, 5.0, 20.0]
        assert [r["pre_close"] for r in hfq] == [9.0, 10.0, 20.0]
        assert [r["change"] for r in hfq] == [1.0, 0.0, 20.0]
        assert [r["pre_close"] for r in qfq] == [4.5, 5.0, 10.0]
        assert [r["change"] for r in qfq] == [0.5, 0.0, 10.0]
        assert [r["pct_chg"] for r in hfq] == [11.11, 0.0, 100.0]
        assert [r["vol"] for r in qfq] == [1000.0, 1000.0, 2000.0]
        assert [r["amount"] for r in qfq] == [10000.0, 10000.0, 40000.0]
    finally:
        db.close_all()


def test_a05_three_price_types_share_same_columns(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    code = _seed_three_day_sample(repo)
    expected = {
        "trade_date", "open", "high", "low", "close",
        "pre_close", "change", "pct_chg", "vol", "amount", "adj_factor",
    }
    try:
        for price_type in ("raw", "qfq", "hfq"):
            rows = repo.daily_query(MARKET, code, "2020-01-01", "2020-01-31", price_type=price_type)
            assert rows, price_type
            assert set(rows[0]) == expected, price_type
            assert len(rows[0]) == 11, price_type
    finally:
        db.close_all()


def test_a06_adj_factor_backfill_reflects_immediately(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    code = _seed_three_day_sample(repo)
    try:
        assert repo.daily_query(MARKET, code, "2020-01-02", "2020-01-02", price_type="qfq")[0]["close"] == 5.0
        assert repo.daily_query(MARKET, code, "2020-01-02", "2020-01-02", price_type="hfq")[0]["close"] == 10.0
        db.execute(
            "UPDATE bar_adj_factor SET adj_factor=? WHERE market=? AND symbol=? AND trade_date=?",
            (4.0, MARKET, code, "2020-01-02"),
        )
        assert repo.daily_query(MARKET, code, "2020-01-02", "2020-01-02", price_type="qfq")[0]["close"] == 20.0
        assert repo.daily_query(MARKET, code, "2020-01-02", "2020-01-02", price_type="hfq")[0]["close"] == 40.0
    finally:
        db.close_all()


def test_a07_raw_keeps_rows_with_missing_adj_factor(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    symbol = "000001.SZ"
    dates = ["2020-01-02", "2020-01-03", "2020-01-06"]
    daily = pd.DataFrame(
        [
            {
                "symbol": symbol, "trade_date": d, "open": 10.0, "high": 11.0, "low": 9.0,
                "close": 10.0, "pre_close": 9.0, "change": 1.0, "pct_chg": 5.0,
                "vol": 1000.0, "amount": 10000.0,
            }
            for d in dates
        ],
        columns=["symbol", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"],
    )
    adj = pd.DataFrame(
        [
            {"symbol": symbol, "trade_date": d, "adj_factor": 1.0}
            for d in dates[:2]
        ],
        columns=["symbol", "trade_date", "adj_factor"],
    )
    repo.upsert_bars(MARKET, daily, source="fake")
    repo.upsert_adj(MARKET, adj, source="fake")
    try:
        rows = repo.daily_query(MARKET, symbol, "2020-01-01", "2020-01-31", price_type="raw")
        assert len(rows) == 3
        missing = [r for r in rows if r["adj_factor"] is None]
        assert len(missing) == 1
        assert missing[0]["trade_date"] == "2020-01-06"
    finally:
        db.close_all()


def test_adj_missing_keeps_qfq_hfq_rows(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    symbol = "000001.SZ"
    dates = ["2020-01-02", "2020-01-03", "2020-01-06"]
    daily = pd.DataFrame(
        [
            {
                "symbol": symbol, "trade_date": d, "open": 10.0, "high": 11.0, "low": 9.0,
                "close": 10.0, "pre_close": 9.0, "change": 1.0, "pct_chg": 5.0,
                "vol": 1000.0, "amount": 10000.0,
            }
            for d in dates
        ],
        columns=["symbol", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"],
    )
    repo.upsert_bars(MARKET, daily, source="fake")
    try:
        for price_type in ("qfq", "hfq"):
            rows = repo.daily_query(MARKET, symbol, "2020-01-01", "2020-01-31", price_type=price_type)
            assert len(rows) == 3, price_type
            assert all(r["close"] == 10.0 for r in rows), price_type
            assert all(r["adj_factor"] == 1.0 for r in rows), price_type
        repo.upsert_adj(
            MARKET,
            pd.DataFrame(
                [
                    {"symbol": symbol, "trade_date": "2020-01-02", "adj_factor": 1.0},
                    {"symbol": symbol, "trade_date": "2020-01-06", "adj_factor": 2.0},
                ],
                columns=["symbol", "trade_date", "adj_factor"],
            ),
            source="fake",
        )
        rows = repo.daily_query(MARKET, symbol, "2020-01-01", "2020-01-31", price_type="qfq")
        assert len(rows) == 3
        assert [r["close"] for r in rows] == [5.0, 10.0, 10.0]
        assert [r["adj_factor"] for r in rows] == [1.0, 2.0, 2.0]
    finally:
        db.close_all()


def test_a10_qfq_query_plan_materializes_anchor(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    code = _seed_three_day_sample(repo)
    sql = """
    WITH anchor AS (
        SELECT adj_factor AS f
        FROM bar_adj_factor
        WHERE market = ? AND symbol = ?
        ORDER BY trade_date DESC LIMIT 1
    )
    SELECT d.trade_date, d.close * a.adj_factor / anchor.f AS close
    FROM bar_daily d
    JOIN bar_adj_factor a
      ON a.market = d.market AND a.symbol = d.symbol AND a.trade_date = d.trade_date
    CROSS JOIN anchor
    WHERE d.market = ? AND d.symbol = ? AND d.trade_date BETWEEN ? AND ?
    ORDER BY d.trade_date LIMIT ?
    """
    try:
        plan = db.query(
            "EXPLAIN QUERY PLAN " + sql,
            (MARKET, code, MARKET, code, "2020-01-01", "2020-01-31", 5000),
        )
        text = " ".join(str(dict(r).get("detail") or "") for r in plan)
        assert "MATERIALIZE" in text and "anchor" in text
        assert "SCAN bar_adj_factor" not in text
    finally:
        db.close_all()


# ======================================================================
# 质量三段式（market 维度）
# ======================================================================

def test_a06_issue_three_stage_scan_keeps_resolved(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    check = "invalid_ohlc"
    issue_a = {"check_type": check, "market": MARKET, "symbol": "600519.SH", "trade_date": "2025-01-02", "detail": "bad"}
    issue_b = {"check_type": check, "market": MARKET, "symbol": "000001.SZ", "trade_date": "2025-01-02", "detail": "bad"}

    repo.mark_scan_start(check, MARKET, "2025-01-01", "2025-01-31")
    repo.insert_issues([issue_a, issue_b])
    repo.purge_stale_issues(check, MARKET, "2025-01-01", "2025-01-31")
    assert repo.db.scalar("SELECT COUNT(*) FROM data_quality_issue") == 2

    ids = [
        r["id"] for r in repo.issues(check_type=check, resolved=0)[0]
        if r["symbol"] == "600519.SH"
    ]
    repo.resolve_issues(ids)
    row = repo.db.query_one(
        "SELECT resolved, resolved_at FROM data_quality_issue WHERE symbol='600519.SH'"
    )
    assert row["resolved"] == 1 and row["resolved_at"] is not None

    repo.mark_scan_start(check, MARKET, "2025-01-01", "2025-01-31")
    repo.insert_issues([{**issue_a, "detail": "bad again"}])
    repo.purge_stale_issues(check, MARKET, "2025-01-01", "2025-01-31")
    rows = repo.db.query("SELECT symbol, resolved, resolved_at FROM data_quality_issue ORDER BY id")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600519.SH"
    assert rows[0]["resolved"] == 0
    assert rows[0]["resolved_at"] is None

    repo.mark_scan_start(check, MARKET, "2025-01-01", "2025-01-31")
    repo.insert_issues([])
    repo.purge_stale_issues(check, MARKET, "2025-01-01", "2025-01-31")
    assert repo.db.scalar("SELECT COUNT(*) FROM data_quality_issue") == 0


def test_purge_stale_scoped_to_scan_range(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    check = "invalid_ohlc"
    issue_outside = {"check_type": check, "market": MARKET, "symbol": "600519.SH", "trade_date": "2025-02-01", "detail": ""}
    issue_inside = {"check_type": check, "market": MARKET, "symbol": "000001.SZ", "trade_date": "2025-05-01", "detail": ""}
    repo.insert_issues([issue_outside, issue_inside])

    repo.mark_scan_start(check, MARKET, "2025-01-01", "2025-12-31")
    assert repo.db.scalar("SELECT COUNT(*) FROM data_quality_issue WHERE detected_at=''") == 2

    repo.mark_scan_start(check, MARKET, "2025-04-01", "2025-06-30")
    repo.insert_issues([{**issue_inside, "detail": "bad again"}])
    repo.purge_stale_issues(check, MARKET, "2025-04-01", "2025-06-30")

    rows = {r["symbol"]: dict(r) for r in repo.db.query("SELECT * FROM data_quality_issue")}
    assert set(rows) == {"600519.SH", "000001.SZ"}
    assert rows["600519.SH"]["detected_at"] == ""
    assert rows["000001.SZ"]["detected_at"] != ""

    repo.mark_scan_start(check, MARKET, "2025-01-01", "2025-12-31")
    repo.insert_issues([])
    repo.purge_stale_issues(check, MARKET, "2025-01-01", "2025-12-31")
    assert repo.db.scalar("SELECT COUNT(*) FROM data_quality_issue") == 0
