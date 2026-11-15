from __future__ import annotations

import threading

import pandas as pd

from conftest import make_db

from yafip.data.market import MARKET_CN_A
from yafip.data.repository import Repository
from yafip.data.staging import (
    DATASET_BARS,
    KIND_BARS,
    StagingStore,
)

MARKET = MARKET_CN_A


def test_concurrent_commits_serialize(tmp_path):
    """DB 提交单写队列：并发 commit 无 database is locked，数据完整。"""
    db = make_db(tmp_path)
    repo = Repository(db)
    store = StagingStore(tmp_path, db, repo)

    def make_unit(batch_id, symbol):
        return store.write_unit(
            batch_id=batch_id, task_id=batch_id, task_type="bars.range",
            executor="tushare.bars.range", source="tushare", market=MARKET,
            dataset=DATASET_BARS, unit_key="2025-01-02", kind=KIND_BARS,
            df=pd.DataFrame(
                [{"symbol": symbol, "trade_date": "2025-01-02", "open": 10.0, "high": 11.0,
                  "low": 9.0, "close": 10.5, "pre_close": 10.0, "vol": 1000.0, "amount": 10500.0}]
            ),
            params_snapshot={},
        )

    units = [(make_unit("b-" + str(i), "60051" + str(i) + ".SH"), KIND_BARS) for i in range(4)]
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def commit(unit):
        try:
            barrier.wait(5)
            store.commit_units(DATASET_BARS, MARKET, [unit],
                               source="tushare",
                               currency="CNY", vol_unit="手", amount_unit="千元")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=commit, args=(u,)) for u in units]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors, errors
    assert repo.bar_count(MARKET) == 4
    # 批次状态全部 applied
    rows = db.query("SELECT status, COUNT(*) AS n FROM staging_batch GROUP BY status")
    counts = {str(r["status"]): int(r["n"]) for r in rows}
    assert counts == {"applied": 4}
