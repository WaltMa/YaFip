from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from conftest import make_db

from yafip.data.market import MARKET_CN_A
from yafip.data.repository import Repository
from yafip.data.staging import (
    APPLIED,
    DATASET_BARS,
    DATASET_META,
    KIND_ADJ,
    KIND_BARS,
    KIND_INSTRUMENT,
    KIND_METRIC,
    VALIDATED,
    StagingError,
    StagingStore,
)

MARKET = MARKET_CN_A


def make_store(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    store = StagingStore(tmp_path, db, repo)
    return db, repo, store


def _bars_df(day="2025-01-02", symbol="600519.SH"):
    return pd.DataFrame(
        [
            {"symbol": symbol, "trade_date": day, "open": 10.0, "high": 11.0, "low": 9.0,
             "close": 10.5, "pre_close": 10.0, "vol": 1000.0, "amount": 10500.0},
        ]
    )


def test_write_unit_creates_manifest_and_batch_row(tmp_path):
    db, repo, store = make_store(tmp_path)
    csv = store.write_unit(
        batch_id="b1", task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={"start": "2025-01-01", "end": "2025-01-31"},
    )
    assert csv.exists()
    manifest = json.loads((csv.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["batch_id"] == "b1"
    unit = manifest["units"][csv.name]
    assert unit["status"] == "created"
    assert unit["rows"] == 1
    row = db.query_one("SELECT * FROM staging_batch WHERE batch_id='b1'")
    assert row["status"] == "created"


def test_validate_bars_rules(tmp_path):
    db, repo, store = make_store(tmp_path)
    csv = store.write_unit(
        batch_id="b2", task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    errors = store.validate_unit(
        csv, market=MARKET, kind=KIND_BARS, start="2025-01-01", end="2025-01-31",
        whitelist_codes={"600519.SH"}, enforce_whitelist=True,
    )
    assert errors == []

    # OHLC 不成立
    bad = csv.parent / "bad_ohlc.csv"
    bad.write_text(
        "symbol,trade_date,open,high,low,close,pre_close,vol,amount\n"
        "600519.SH,2025-01-02,10,9,11,10.5,10,1000,10500\n",
        encoding="utf-8",
    )
    errors = store.validate_unit(
        bad, market=MARKET, kind=KIND_BARS, start="2025-01-01", end="2025-01-31",
        whitelist_codes={"600519.SH"}, enforce_whitelist=True,
    )
    assert any("OHLC" in e for e in errors)

    # 非白名单
    other = csv.parent / "outside.csv"
    other.write_text(
        "symbol,trade_date,open,high,low,close,pre_close,vol,amount\n"
        "000001.SZ,2025-01-02,10,11,9,10.5,10,1000,10500\n",
        encoding="utf-8",
    )
    errors = store.validate_unit(
        other, market=MARKET, kind=KIND_BARS, start="2025-01-01", end="2025-01-31",
        whitelist_codes={"600519.SH"}, enforce_whitelist=True,
    )
    assert any("白名单" in e for e in errors)
    # range 显式 symbols 豁免
    errors = store.validate_unit(
        other, market=MARKET, kind=KIND_BARS, start="2025-01-01", end="2025-01-31",
        whitelist_codes={"600519.SH"}, enforce_whitelist=False,
    )
    assert errors == []

    # 日期越界
    oob = csv.parent / "oob.csv"
    oob.write_text(
        "symbol,trade_date,open,high,low,close,pre_close,vol,amount\n"
        "600519.SH,2025-02-01,10,11,9,10.5,10,1000,10500\n",
        encoding="utf-8",
    )
    errors = store.validate_unit(
        oob, market=MARKET, kind=KIND_BARS, start="2025-01-01", end="2025-01-31",
    )
    assert any("区间" in e for e in errors)


def test_commit_applies_atomically(tmp_path):
    db, repo, store = make_store(tmp_path)
    batch_id = "b3"
    bars_csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    adj_df = pd.DataFrame([{"symbol": "600519.SH", "trade_date": "2025-01-02", "adj_factor": 1.0}])
    adj_csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_ADJ, df=adj_df, params_snapshot={},
    )
    store.commit_units(
        DATASET_BARS, MARKET,
        [(bars_csv, KIND_BARS), (adj_csv, KIND_ADJ)],
        source="tushare", currency="CNY", vol_unit="手", amount_unit="千元",
    )
    assert repo.bar_count() == 1
    assert repo.adj_count() == 1
    row = db.query_one("SELECT * FROM staging_batch WHERE batch_id=?", (batch_id,))
    assert row["status"] == "applied"
    manifest = json.loads((bars_csv.parent / "manifest.json").read_text(encoding="utf-8"))
    assert all(u["status"] == APPLIED for u in manifest["units"].values())


def test_commit_failure_rolls_back(tmp_path):
    db, repo, store = make_store(tmp_path)
    batch_id = "b4"
    bars_csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    adj_csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_ADJ, df=pd.DataFrame([{"symbol": "600519.SH", "trade_date": "2025-01-02", "adj_factor": 1.0}]),
        params_snapshot={},
    )
    # 删除 adj 文件：commit 读取失败 → 整体回滚，bar_daily 零行（原子性）
    adj_csv.unlink()
    try:
        store.commit_units(
            DATASET_BARS, MARKET,
            [(bars_csv, KIND_BARS), (adj_csv, KIND_ADJ)],
            source="tushare", currency="CNY", vol_unit="手", amount_unit="千元",
        )
        raise AssertionError("应抛出 StagingError")
    except StagingError:
        pass
    assert repo.bar_count() == 0
    manifest = json.loads((bars_csv.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["units"][bars_csv.name]["status"] == "failed"
    assert manifest["units"][adj_csv.name]["status"] == "failed"


def test_meta_units_commit(tmp_path):
    db, repo, store = make_store(tmp_path)
    batch_id = "b5"
    inst_csv = store.write_unit(
        batch_id=batch_id, task_id="t2", task_type="meta.sync_all", executor="tushare.meta.sync_all",
        source="tushare", market=MARKET, dataset=DATASET_META, unit_key="meta",
        kind=KIND_INSTRUMENT,
        df=pd.DataFrame([
            {"symbol": "600519.SH", "name": "贵州茅台", "asset_type": "STOCK",
             "exchange": "SSE", "currency": "CNY", "list_date": "2001-08-27",
             "delist_date": None, "status": "L", "ext_json": None},
        ]),
        params_snapshot={},
    )
    metric_csv = store.write_unit(
        batch_id=batch_id, task_id="t2", task_type="meta.sync_all", executor="tushare.meta.sync_all",
        source="tushare", market=MARKET, dataset=DATASET_META, unit_key="meta",
        kind=KIND_METRIC,
        df=pd.DataFrame([
            {"symbol": "600519.SH", "metric_key": "total_mv", "metric_value": 2000000.0,
             "unit": "万元", "as_of_date": "2026-11-13"},
        ]),
        params_snapshot={},
    )
    store.commit_units(
        DATASET_META, MARKET,
        [(inst_csv, KIND_INSTRUMENT), (metric_csv, KIND_METRIC)],
        source="tushare",
    )
    assert repo.get_instrument(MARKET, "600519.SH")["name"] == "贵州茅台"
    metric = db.query_one("SELECT * FROM instrument_metric WHERE market=? AND symbol=?", (MARKET, "600519.SH"))
    assert metric["metric_value"] == 2000000.0
    assert metric["unit"] == "万元"


def test_reusable_and_cleanup(tmp_path):
    db, repo, store = make_store(tmp_path)
    # bars incremental batch：applied，无水位 → 保留（水位保护不是"有水位才保护"）
    batch_id = "b6"
    csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.incremental", executor="tushare.bars.incremental",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    store.commit_units(DATASET_BARS, MARKET, [(csv, KIND_BARS)],
                       source="tushare", currency="CNY", vol_unit="手", amount_unit="千元")
    # 重跑复用：applied 文件可被找到（按任务类型/执行器/标的过滤）
    reusable = store.reusable_units(
        DATASET_BARS, MARKET, kinds=(KIND_BARS,),
        task_type="bars.incremental", executor="tushare.bars.incremental",
        symbols={"600519.SH"},
    )
    assert any(u["unit_key"] == "2025-01-02" for u in reusable)
    # 不同执行器 / 不同标的集合 → 不复用
    assert store.reusable_units(DATASET_BARS, MARKET, kinds=(KIND_BARS,),
                                task_type="bars.incremental", executor="akshare.bars.incremental",
                                symbols={"600519.SH"}) == []
    assert store.reusable_units(DATASET_BARS, MARKET, kinds=(KIND_BARS,),
                                task_type="bars.incremental", executor="tushare.bars.incremental",
                                symbols={"000001.SZ"}) == []

    # 无水位（sync_state 0 行）→ cleanup 保留（核心修复）
    removed = store.cleanup(30, {})
    assert removed == 0
    assert csv.exists()
    # 水位未覆盖 → 保留
    removed = store.cleanup(30, {MARKET: "2025-01-01"})
    assert removed == 0
    assert csv.exists()

    # 水位覆盖 → 可清理
    removed = store.cleanup(30, {MARKET: "2025-01-03"})
    assert removed == 1
    assert not csv.exists()


def test_range_applied_cleaned_even_without_watermark(tmp_path):
    """range applied 即清（不依赖水位）；incremental 未覆盖才保留。"""
    db, repo, store = make_store(tmp_path)
    range_csv = store.write_unit(
        batch_id="r1", task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    store.commit_units(DATASET_BARS, MARKET, [(range_csv, KIND_BARS)],
                       source="tushare", currency="CNY", vol_unit="手", amount_unit="千元")
    removed = store.cleanup(30, {})  # 无水位
    assert removed == 1
    assert not range_csv.exists()


def test_validated_unit_lifecycle_and_cross_batch_commit(tmp_path):
    """校验通过标 VALIDATED → 可复用；被 kill 的旧 batch validated 文件可跨 batch 提交。"""
    db, repo, store = make_store(tmp_path)
    # 模拟旧任务（被 kill）：写单元 → 校验 → 标 VALIDATED，未提交
    old_batch = "old-killed"
    bars_csv = store.write_unit(
        batch_id=old_batch, task_id="t-old", task_type="bars.incremental",
        executor="tushare.bars.incremental",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    assert store.validate_unit(bars_csv, market=MARKET, kind=KIND_BARS) == []
    store.mark_unit_status(DATASET_BARS, MARKET, old_batch, bars_csv.name, "validated")

    # 可复用（与 fetcher 相同过滤条件）
    reusable = store.reusable_units(
        DATASET_BARS, MARKET, kinds=(KIND_BARS,),
        task_type="bars.incremental", executor="tushare.bars.incremental",
        symbols={"600519.SH"},
    )
    assert len(reusable) == 1 and reusable[0]["status"] == "validated"

    # 新任务提交时把旧 batch 的 validated 文件一起提交（跨 batch，单事务）
    store.commit_units(DATASET_BARS, MARKET, [(bars_csv, KIND_BARS)],
                       source="tushare", currency="CNY", vol_unit="手", amount_unit="千元")
    assert repo.bar_count() == 1
    manifest = json.loads((bars_csv.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["units"][bars_csv.name]["status"] == "applied"
    # 提交后 cleanup：incremental 无水位 → 仍保留
    assert store.cleanup(30, {}) == 0


def test_batch_checksum_is_manifest_fingerprint(tmp_path):
    """checksum 是 manifest 指纹，随状态变化，不再写死空串。"""
    db, repo, store = make_store(tmp_path)
    batch_id = "b-checksum"
    csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    manifest = json.loads((csv.parent / "manifest.json").read_text(encoding="utf-8"))
    expected = hashlib.sha1(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    row = db.query_one("SELECT checksum FROM staging_batch WHERE batch_id=?", (batch_id,))
    assert row["checksum"] == expected
    assert row["checksum"] and row["checksum"] != ""

    store.mark_unit_status(DATASET_BARS, MARKET, batch_id, csv.name, VALIDATED)
    updated = db.query_one("SELECT checksum FROM staging_batch WHERE batch_id=?", (batch_id,))
    assert updated["checksum"] != row["checksum"]


def test_cleanup_deletes_staging_batch_row(tmp_path):
    """cleanup 删目录时同步删除登记行，避免 staging_batch 只增不减。"""
    db, repo, store = make_store(tmp_path)
    batch_id = "b-cleanup"
    csv = store.write_unit(
        batch_id=batch_id, task_id="t1", task_type="bars.range", executor="tushare.bars.range",
        source="tushare", market=MARKET, dataset=DATASET_BARS, unit_key="2025-01-02",
        kind=KIND_BARS, df=_bars_df(), params_snapshot={},
    )
    store.commit_units(DATASET_BARS, MARKET, [(csv, KIND_BARS)],
                       source="tushare", currency="CNY", vol_unit="手", amount_unit="千元")
    before = db.scalar("SELECT COUNT(*) FROM staging_batch")
    assert db.scalar("SELECT COUNT(*) FROM staging_batch WHERE batch_id=?", (batch_id,)) == 1

    assert store.cleanup(30, {}) == 1
    assert not csv.exists()
    after = db.scalar("SELECT COUNT(*) FROM staging_batch")
    assert after == before - 1
    assert db.scalar("SELECT COUNT(*) FROM staging_batch WHERE batch_id=?", (batch_id,)) == 0
