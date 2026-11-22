"""tushare A 股端到端冒烟。

- 需真实 token：环境变量 `YAFIP_TUSHARE_TOKEN`（仅测试用；产品路径从「数据源配置」页读取）；
- 无 token / 断网时自动 skip，不影响常规 CI；
- 只做小日期区间，避免触发限频惩罚窗口。
"""
from __future__ import annotations

import os

import pytest

TOKEN = os.environ.get("YAFIP_TUSHARE_TOKEN", "").strip()

pytestmark = pytest.mark.skipif(not TOKEN, reason="未设置 YAFIP_TUSHARE_TOKEN（真实接口冒烟）")


def _source():
    from yafip.config import ConfigService
    from yafip.core.db import Database
    from yafip.data.repository import Repository
    from yafip.data.tushare_source import TushareSource

    db = Database(":memory:")
    db.init_schema()
    repo = Repository(db)
    config = ConfigService(db)
    repo.save_datasource("tushare", token=TOKEN)
    return TushareSource(repo, config)


@pytest.mark.slow
def test_real_calendar_small_range():
    source = _source()
    df = source.get_calendar("CN_A", "2026-11-01", "2026-11-15")
    assert not df.empty
    assert "cal_date" in df.columns


@pytest.mark.slow
def test_real_daily_one_date():
    source = _source()
    df = source.get_daily("CN_A", "2026-11-13")
    if df is not None and not df.empty:
        assert "ts_code" in df.columns
        assert "close" in df.columns


@pytest.mark.slow
def test_real_adj_factor_one_date():
    source = _source()
    df = source.get_adj_factor("CN_A", "2026-11-13")
    if df is not None and not df.empty:
        assert "adj_factor" in df.columns


@pytest.mark.slow
def test_real_stock_list():
    source = _source()
    df = source.get_stock_list("CN_A")
    assert not df.empty
    assert "ts_code" in df.columns


@pytest.mark.slow
def test_real_meta_chain_via_handlers(tmp_path):
    """端到端链路（离线可测部分）：meta.sync_all → instrument + metric 落库。"""
    from conftest import FakeContext
    from yafip.config import ConfigService
    from yafip.core.audit import AuditService
    from yafip.core.db import Database
    from yafip.data.market import MARKET_CN_A
    from yafip.data.repository import Repository
    from yafip.data.staging import StagingStore
    from yafip.data.tasks.handlers import build_task_handlers
    from yafip.data.tushare_source import TushareSource
    from yafip.data.whitelist import WhitelistService

    db = Database(tmp_path / "e2e.db")
    db.init_schema()
    repo = Repository(db)
    config = ConfigService(db)
    audit = AuditService(db)
    source = TushareSource(repo, config)
    repo.save_datasource("tushare", token=TOKEN)
    whitelist = WhitelistService(db, repo, config, audit, source)
    staging = StagingStore(tmp_path, db, repo)
    handlers = build_task_handlers(
        config=config, repo=repo, source=source, staging=staging,
        fetcher=None, whitelist=whitelist,
    )
    result = handlers["tasks.meta_sync_all"](FakeContext("e2e-meta"), {"market": MARKET_CN_A})
    assert result["instrument_rows"] > 0
    assert repo.instrument_count(MARKET_CN_A) > 0
    metric = db.query_one(
        "SELECT * FROM instrument_metric WHERE market=? AND metric_key='total_mv' LIMIT 1",
        (MARKET_CN_A,),
    )
    if metric is not None:
        assert metric["unit"] == "万元"
    db.close_all()
