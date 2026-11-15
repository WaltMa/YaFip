from __future__ import annotations

import pytest

from conftest import FakeContext, FakeSource, make_db, seed_calendar

from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.data.market import MARKET_CN_A
from yafip.data.repository import Repository
from yafip.data.whitelist import (
    WhitelistError,
    WhitelistMemberExists,
    WhitelistMemberNotFound,
    WhitelistService,
)

MARKET = MARKET_CN_A

# 市值快照（万元）：600519 80 亿 / 000001 60 亿 / 000002 40 亿
MV = {
    "600519.SH": 800000.0,
    "000001.SZ": 600000.0,
    "000002.SZ": 400000.0,
}
AS_OF = "2025-01-06"


def seed_metrics(db, repo, as_of: str = AS_OF):
    for symbol, value in MV.items():
        repo.upsert_instrument(MARKET, [{"symbol": symbol, "name": "股票" + symbol[:6], "asset_type": "STOCK", "status": "L"}], source="tushare")
        repo.upsert_instrument_metric(MARKET, symbol, "total_mv", value, unit="万元", as_of_date=as_of, source="tushare")


def setup_service(tmp_path, with_metrics: bool = True):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    audit = AuditService(db)
    source = FakeSource()
    service = WhitelistService(db, repo, config, audit, source)
    seed_calendar(db, MARKET)
    if with_metrics:
        seed_metrics(db, repo)
    return db, config, repo, service


def test_config_defaults_and_update(tmp_path):
    db, config, repo, service = setup_service(tmp_path)

    view = service.config_view(MARKET)
    assert view["metric"] == "total_mv"
    assert view["threshold_wan"] == 500000.0
    assert view["threshold_yi"] == 50.0

    updated = service.update_config({"threshold_yi": 60})
    assert updated["threshold_wan"] == 600000.0
    assert config.get("whitelist.threshold_wan") == "600000.0"

    with pytest.raises(WhitelistError):
        service.update_config({"metric": "total_mv_max"})
    with pytest.raises(WhitelistError):
        service.update_config({"threshold_wan": 0})


def test_recompute_reads_table_and_upserts_only_matching(tmp_path):
    db, config, repo, service = setup_service(tmp_path)

    result = service.recompute_handler(FakeContext("wl-1"), {"market": MARKET})
    assert result["as_of_date"] == AS_OF
    assert result["total_count"] == 3
    assert result["hit_count"] == 2  # 600519 / 000001

    rows = {r["symbol"]: r for r in db.query("SELECT * FROM whitelist_member")}
    assert set(rows) == {"000001.SZ", "600519.SH"}  # 只进不出：未命中不写落选行
    assert rows["600519.SH"]["metric_value"] == 800000.0
    assert rows["600519.SH"]["source"] == "rule"
    assert rows["600519.SH"]["rule_key"] == "total_mv_>_500000"
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='whitelist.recompute'") == 1

    # 提高门槛后重算：只 upsert 命中，已有成员保留（不删除、不落选）
    service.update_config({"threshold_yi": 70})
    second = service.recompute_handler(FakeContext("wl-2"), {"market": MARKET})
    assert second["hit_count"] == 1
    assert repo.member_count(MARKET) == 2
    assert repo.whitelist_codes(MARKET) == ["000001.SZ", "600519.SH"]


def test_recompute_missing_metrics_fails_with_guidance(tmp_path):
    db, config, repo, service = setup_service(tmp_path, with_metrics=False)
    with pytest.raises(WhitelistError, match="meta.sync_all"):
        service.recompute_handler(FakeContext("wl-fail"), {"market": MARKET})
    assert repo.member_count(MARKET) == 0


def test_recompute_no_hit_keeps_whitelist_unchanged(tmp_path):
    db, config, repo, service = setup_service(tmp_path)
    service.recompute_handler(FakeContext("wl-ok"), {"market": MARKET})

    # 无命中：白名单不变，不删除任何条目
    result = service.recompute_handler(
        FakeContext("wl-none"), {"market": MARKET, "value": 900000}
    )
    assert result["hit_count"] == 0
    assert repo.member_count(MARKET) == 2


def test_state_and_empty(tmp_path):
    db, config, repo, service = setup_service(tmp_path)
    assert service.state(MARKET) == set()

    service.recompute_handler(FakeContext("wl-state"), {"market": MARKET})
    assert service.state(MARKET) == {"000001.SZ", "600519.SH"}


def test_member_crud_without_rule(tmp_path):
    db, config, repo, service = setup_service(tmp_path)

    member = service.add_member(MARKET, "600519.SH", reason="手动维护")
    assert member["symbol"] == "600519.SH"
    assert member["source"] == "manual"
    assert repo.member_count() == 1

    with pytest.raises(WhitelistMemberExists):
        service.add_member(MARKET, "600519.sh")

    updated = service.update_member(MARKET, "600519.SH", reason="")
    assert updated["reason"] is None

    items, total = service.members(market=MARKET, keyword="600519")
    assert total == 1
    assert items[0]["symbol"] == "600519.SH"

    service.delete_member(MARKET, "600519.SH")
    assert repo.member_count() == 0
    assert db.scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'whitelist.member_%'"
    ) == 3

    with pytest.raises(WhitelistError):
        service.add_member(MARKET, "600519")
    with pytest.raises(WhitelistMemberNotFound):
        service.update_member(MARKET, "000002.SZ", reason="x")
    with pytest.raises(WhitelistMemberNotFound):
        service.delete_member(MARKET, "000002.SZ")
