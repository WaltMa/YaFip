"""27 条核验问题修复的回归测试（数据层/服务层新用例集中放置）。

每个用例对应 doc.md 中的修复项：D1/D2（本文件 Task 2 批次）、D3、D4、D5/D6、
T1~T3、C1/C2、X3 分批追加。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FakeContext, FakeSource, make_db, seed_calendar

from yafip.api.responses import install_error_handlers
from yafip.config import ConfigError, ConfigService
from yafip.core.audit import AuditService
from yafip.core.db import Database
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter, TaskNotKillable, TaskSpec
from yafip.data.base import ADJ_COLUMNS, DAILY_COLUMNS
from yafip.data.executors import ExecutorError, build_default_registry
from yafip.data.fetcher import Fetcher
from yafip.data.market import MARKET_CN_A, MARKET_HK, MarketError
from yafip.data.quality import QualityService
from yafip.data.repository import Repository
from yafip.data.service import DataService
from yafip.data.staging import (
    DATASET_BARS,
    DATASET_META,
    KIND_ADJ,
    KIND_INSTRUMENT,
    KIND_METRIC,
    StagingStore,
)
from yafip.data.tasks.meta import _sync as meta_sync
from yafip.data.tasks.policy import market_closed, market_local_date, market_local_datetime
from yafip.data.whitelist import WhitelistError, WhitelistService

MARKET = MARKET_CN_A


def build_fetcher(tmp_path):
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


def build_quality(tmp_path) -> tuple[Database, QualityService]:
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    whitelist = WhitelistService(db, repo, config, AuditService(db), FakeSource())
    quality = QualityService(db, repo, config, whitelist)
    return db, quality


# ======================================================================
# D3b 复权因子 0/负数必须被 staging 校验拒绝
# ======================================================================

def test_staging_rejects_nonpositive_adj_factor(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    staging = StagingStore(tmp_path, db, repo)
    for bad in (0.0, -1.0):
        df = pd.DataFrame(
            [{"symbol": "600519.SH", "trade_date": "2025-01-02", "adj_factor": bad}],
            columns=["symbol", "trade_date", "adj_factor"],
        )
        csv = staging.write_unit(
            batch_id="b-adj-bad", task_id="t", task_type="bars.incremental",
            executor="fake.bars.incremental", source="fake", market=MARKET,
            dataset=DATASET_BARS, unit_key="2025-01-02", kind=KIND_ADJ,
            df=df, params_snapshot={},
        )
        errors = staging.validate_unit(
            csv, market=MARKET, kind=KIND_ADJ, adj_policy=("FACTOR", None)
        )
        assert any("必须大于 0" in e for e in errors), f"factor={bad} 应被拒绝：{errors}"


# ======================================================================
# C1 配置拒绝 nan/inf 毒值
# ======================================================================

def test_config_rejects_nan_inf(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    with pytest.raises(ConfigError):
        config.set_many({"whitelist.threshold_wan": "nan"})
    with pytest.raises(ConfigError):
        config.set_many({"fetch.rpm": "inf"})
    with pytest.raises(ConfigError):
        config.set_many({"task.cpu_ratio": float("nan")})


def test_whitelist_rejects_nan_threshold(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    service = WhitelistService(db, repo, ConfigService(db), AuditService(db))
    with pytest.raises(WhitelistError):
        service.update_config({"threshold_wan": float("nan")})
    with pytest.raises(WhitelistError):
        service.update_config({"threshold_yi": float("inf")})


# ======================================================================
# C2 MarketError / ConfigError 错误码
# ======================================================================

def test_market_and_config_error_codes():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom/market")
    def boom_market():
        raise MarketError("未知市场：XX")

    @app.get("/boom/config")
    def boom_config():
        raise ConfigError("非法配置")

    with TestClient(app) as client:
        r = client.get("/boom/market")
        assert r.status_code == 400
        assert r.json()["code"] == 6001  # 修复前未注册 → 500 并回显 MarketError
        r2 = client.get("/boom/config")
        assert r2.status_code == 400
        assert r2.json()["code"] == 1001  # 修复前 ConfigError 无 code → 9999


# ======================================================================
# T1 kill 竞态：已完成任务不能被改成 STOPPING/KILLED
# ======================================================================

def _mk_spec(handler: str, lock_key: str) -> TaskSpec:
    return TaskSpec(
        task_type="demo", executor="fake.demo", market=None, source="manual",
        params={}, handler=handler, lock_key=lock_key,
    )


def test_kill_completed_task_raises_not_killable(tmp_path):
    import time

    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=2)
    center.register("ok", lambda ctx, params: {"value": 1})
    task_id = center.submit(_mk_spec("ok", "demo"))
    deadline = time.time() + 5.0
    while time.time() < deadline and center.get(task_id)["status"] != "SUCCESS":
        time.sleep(0.02)
    assert center.get(task_id)["status"] == "SUCCESS"
    with pytest.raises(TaskNotKillable):
        center.kill(task_id)
    # 修复前：kill 会把已 SUCCESS 的任务改成 STOPPING（永久卡死、状态机破坏）
    assert center.get(task_id)["status"] == "SUCCESS"


def _insert_task(db, task_id: str, status: str, lock_key: str = "demo") -> None:
    db.execute(
        """
        INSERT INTO task(id, task_type, executor, market, source, lock_key, params, handler, status, progress, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (task_id, "demo", "fake.demo", None, "manual", lock_key, "{}", "ok", status, 50, "2026-01-01T00:00:00"),
    )


def test_kill_stopping_is_idempotent(tmp_path):
    """重复 kill 一个已 STOPPING 的任务应幂等成功，不抛 409。"""
    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=2)
    _insert_task(db, "stop1", "STOPPING")
    assert center.kill("stop1") is True
    assert center.get("stop1")["status"] == "STOPPING"


def test_mark_stopping_idempotent_when_already_stopping(tmp_path):
    """0 行分支重读 STOPPING → 幂等 True（PENDING→RUNNING 竞态路径）。"""
    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=2)
    _insert_task(db, "stop2", "STOPPING")
    assert center._mark_stopping("stop2") is True
    assert center.get("stop2")["status"] == "STOPPING"


# ======================================================================
# T2 recover 处理 PENDING；shutdown 不留 PENDING
# ======================================================================

def test_recover_marks_pending_as_failed(tmp_path):
    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=2)
    db.execute(
        """
        INSERT INTO task(id, task_type, executor, market, source, lock_key, params, handler, status, progress, created_at)
        VALUES('pend1','demo','fake.demo',NULL,'manual','demo','{}','ok','PENDING',0,'2026-01-01T00:00:00')
        """
    )
    assert center.recover() == 1
    assert center.get("pend1")["status"] == "FAILED"


def test_shutdown_leaves_no_pending(tmp_path):
    import time

    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=2)

    def slow(ctx, params):
        for _ in range(200):
            ctx.checkpoint()
            time.sleep(0.01)
        return {}

    center.register("slow", slow)
    task_id = center.submit(_mk_spec("slow", "demo-shutdown"))
    deadline = time.time() + 5.0
    while time.time() < deadline and center.get(task_id)["status"] != "RUNNING":
        time.sleep(0.02)
    center.shutdown(wait_timeout=0.0)
    record = center.get(task_id)
    # 修复前：RUNNING 任务留在 RUNNING（非终态），进程退出后永远占着 lock_key
    assert record["status"] in ("FAILED", "KILLED", "SUCCESS")


# ======================================================================
# C 任务参数校验：nan/inf/非法日期必须被拒（修复前能绕过）
# ======================================================================

def test_validate_params_rejects_nan_inf_and_bad_date():
    registry = build_default_registry()
    # number：nan/inf 能通过大小比较（nan<min 恒 False），修复前规则 0 命中但任务成功
    for bad in ("nan", "inf", "-inf"):
        with pytest.raises(ExecutorError):
            registry.validate_submission(
                "whitelist.update", "tushare.whitelist.update", MARKET_CN_A,
                {"metric": "total_mv", "op": ">", "value": bad},
            )
    # date：2026-99-99 修复前只查 YYYY-- 占位被放行 → 静默 0 数据
    with pytest.raises(ExecutorError):
        registry.validate_submission(
            "bars.range", "tushare.bars.range", MARKET_CN_A,
            {"start": "2026-99-99", "end": "2026-12-31"},
        )
    # 合法值仍通过
    registry.validate_submission(
        "bars.range", "tushare.bars.range", MARKET_CN_A,
        {"start": "2026-11-20", "end": "2026-12-31"},
    )


# ======================================================================
# meta.sync_symbols：DataService.submit 必须注入真实 task_type
# （否则 meta._sync 兜底成 meta.sync_all，manifest 误标、与文档隔离语义不符）
# ======================================================================

def test_data_service_submit_injects_task_type(tmp_path):
    import json

    db = make_db(tmp_path)
    config = ConfigService(db)
    center = TaskCenter(db, SSEBroker(), max_workers=2)
    registry = build_default_registry()
    center.register("tasks.meta_sync_symbols", lambda ctx, params: {})
    data = DataService(center, registry, config)

    task_id = data.submit(
        "meta.sync_symbols", "tushare.meta.sync_symbols", MARKET_CN_A,
        {"symbols": ["600519.SH"]},
    )
    params = json.loads(center.get(task_id)["params"])
    assert params["task_type"] == "meta.sync_symbols"  # 修复前缺失 → meta._sync 兜底成 meta.sync_all
    assert params["market"] == MARKET_CN_A
    assert params["executor"] == "tushare.meta.sync_symbols"


# ======================================================================
# X3 meta kill 后重跑复用 validated 单元
# ======================================================================

def _write_meta_validated_units(
    staging,
    ctx_id: str,
    *,
    with_metric: bool,
    symbol: str = "600519.SH",
    task_type: str = "meta.sync_all",
    executor: str = "fake.meta.sync_all",
):
    inst_df = pd.DataFrame(
        [{
            "symbol": symbol, "name": "贵州茅台", "asset_type": "STOCK",
            "exchange": "SSE", "currency": "CNY", "list_date": "2025-01-01",
            "delist_date": None, "status": "L", "ext_json": None,
        }],
        columns=["symbol", "name", "asset_type", "exchange", "currency",
                 "list_date", "delist_date", "status", "ext_json"],
    )
    inst_csv = staging.write_unit(
        batch_id=ctx_id, task_id=ctx_id, task_type=task_type,
        executor=executor, source="fake", market=MARKET,
        dataset=DATASET_META, unit_key="meta", kind=KIND_INSTRUMENT,
        df=inst_df, params_snapshot={},
    )
    staging.mark_unit_status(DATASET_META, MARKET, ctx_id, inst_csv.name, "validated")
    units = 1
    if with_metric:
        metric_df = pd.DataFrame(
            [{"symbol": symbol, "metric_key": "total_mv", "metric_value": 1000000.0,
              "unit": "万元", "as_of_date": "2025-01-06"}],
            columns=["symbol", "metric_key", "metric_value", "unit", "as_of_date"],
        )
        metric_csv = staging.write_unit(
            batch_id=ctx_id, task_id=ctx_id, task_type=task_type,
            executor=executor, source="fake", market=MARKET,
            dataset=DATASET_META, unit_key="meta", kind=KIND_METRIC,
            df=metric_df, params_snapshot={},
        )
        staging.mark_unit_status(DATASET_META, MARKET, ctx_id, metric_csv.name, "validated")
        units = 2
    return units


def test_meta_sync_symbols_reuse_isolated_by_symbols(tmp_path, monkeypatch):
    """I-11：meta.sync_symbols 复用必须按标的集合全等隔离（修复前会复用他人标的的单元）。"""
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    source = FakeSource()
    staging = StagingStore(tmp_path, db, repo)
    seed_calendar(db, MARKET)
    # 遗留：600519.SH 的 validated 单元（同 task_type/executor）
    _write_meta_validated_units(
        staging, "old-syms", with_metric=True,
        symbol="600519.SH", task_type="meta.sync_symbols", executor="fake.meta.sync_symbols",
    )
    called: list[int] = []
    real_list = source.get_stock_list

    def counting(market):
        called.append(1)
        return real_list(market)

    monkeypatch.setattr(source, "get_stock_list", counting)
    result = meta_sync(
        FakeContext("meta-msft"),
        config=config, repo=repo, source=source, staging=staging,
        params={"market": MARKET, "task_type": "meta.sync_symbols", "executor": "fake.meta.sync_symbols"},
        symbols={"000001.SZ"},
    )
    # 修复前：直接复用 600519 的单元（DB 写错标的）；修复后：不复用、重新请求源抓 000001.SZ
    assert "reused_units" not in result
    assert called, "标的集合不同时不得复用他人单元的 validated 文件"
    assert repo.get_instrument(MARKET, "000001.SZ") is not None
    assert repo.get_instrument(MARKET, "600519.SH") is None


def test_meta_retry_reuses_validated_units(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    source = FakeSource()
    staging = StagingStore(tmp_path, db, repo)

    _write_meta_validated_units(staging, "killed-meta", with_metric=True)

    def boom(*_args, **_kwargs):
        raise AssertionError("复用路径不应再次请求数据源")

    monkeypatch.setattr(source, "get_stock_list", boom)
    monkeypatch.setattr(source, "get_daily_basic", boom)

    result = meta_sync(
        FakeContext("meta-retry"),
        config=config, repo=repo, source=source, staging=staging,
        params={"market": MARKET, "task_type": "meta.sync_all", "executor": "fake.meta.sync_all"},
        symbols=None,
    )
    assert result["reused_units"] == 2
    assert repo.get_instrument(MARKET, "600519.SH") is not None
    assert db.scalar(
        "SELECT COUNT(*) FROM instrument_metric WHERE market=? AND symbol='600519.SH'",
        (MARKET,),
    ) == 1


def test_cleanup_removes_expired_meta_validated(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    staging = StagingStore(tmp_path, db, repo)
    _write_meta_validated_units(staging, "old-meta", with_metric=False)

    # 伪造创建时间为 40 天前（retention=30）
    manifest_path = staging._manifest_path(DATASET_META, MARKET, "old-meta")
    import json as _json

    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    manifest_path.write_text(_json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    removed = staging.cleanup(30, {})
    assert removed == 1
    assert not manifest_path.exists()


# ======================================================================
# D1 quality._missing_days 占位符参数序
# ======================================================================

def test_quality_missing_days_finds_real_missing(tmp_path):
    """缺失交易日检查能真正查出缺失（占位符参数序修正后）。"""
    db, quality = build_quality(tmp_path)
    seed_calendar(db, MARKET, ["2025-01-02", "2025-01-03", "2025-01-06"])
    repo = Repository(db)
    repo.upsert_instrument(
        MARKET,
        [{"symbol": "600519.SH", "name": "贵州茅台", "asset_type": "STOCK",
          "exchange": "SSE", "currency": "CNY", "list_date": "2025-01-01",
          "delist_date": None, "status": "L", "ext_json": None}],
        source="fake",
    )
    # 只有 01-02 与 01-06 有 bar，01-03 缺失
    repo.upsert_bars(
        MARKET,
        pd.DataFrame([
            {"symbol": "600519.SH", "trade_date": "2025-01-02", "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 10.5, "pre_close": 10.0, "vol": 1000.0, "amount": 10500.0},
            {"symbol": "600519.SH", "trade_date": "2025-01-06", "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 10.5, "pre_close": 10.0, "vol": 1000.0, "amount": 10500.0},
        ]),
        source="fake",
    )
    issues = quality._missing_days(MARKET, "2025-01-01", "2025-01-31", ["600519.SH"])
    assert len(issues) == 1
    assert issues[0]["check_type"] == "missing_days"
    assert issues[0]["symbol"] == "600519.SH"
    assert "missing=1/3" in issues[0]["detail"]


def test_quality_missing_days_respects_list_date(tmp_path):
    """区间收敛到 list_date，上市前不算缺失。"""
    db, quality = build_quality(tmp_path)
    seed_calendar(db, MARKET, ["2025-01-02", "2025-01-03", "2025-01-06"])
    repo = Repository(db)
    repo.upsert_instrument(
        MARKET,
        [{"symbol": "600519.SH", "name": "贵州茅台", "asset_type": "STOCK",
          "exchange": "SSE", "currency": "CNY", "list_date": "2025-01-06",
          "delist_date": None, "status": "L", "ext_json": None}],
        source="fake",
    )
    repo.upsert_bars(
        MARKET,
        pd.DataFrame([
            {"symbol": "600519.SH", "trade_date": "2025-01-06", "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 10.5, "pre_close": 10.0, "vol": 1000.0, "amount": 10500.0},
        ]),
        source="fake",
    )
    issues = quality._missing_days(MARKET, "2025-01-01", "2025-01-31", ["600519.SH"])
    assert issues == []  # 01-06 上市首日有 bar，01-02/03 在上市前不计缺失


# ======================================================================
# D2a 源有数据但 0 命中 → 当日失败、不推水位
# ======================================================================
def test_fetcher_zero_hit_fails_and_keeps_watermark(tmp_path, monkeypatch):
    db, config, repo, whitelist, fetcher = build_fetcher(tmp_path)
    source = fetcher.source
    real_daily = source.get_daily

    def only_uncovered(market, trade_date, symbols=None):
        # 源返回一个白名单外的代码（格式合法但源不覆盖）
        df = real_daily(market, trade_date, symbols=symbols)
        temp = df.copy()
        temp["ts_code"] = "999999.SZ"
        return temp

    monkeypatch.setattr(source, "get_daily", only_uncovered)
    result = fetcher.run(FakeContext("t0"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    assert result["dates_done"] == 0
    assert result["dates_failed"] == 3
    assert "0 命中" in result["failed_units"][0]["error"]
    # 不推进水位：白名单坏标的移除/修复前，这段历史不会被永久跳过
    assert repo.sync_state_watermark(MARKET) is None
    assert repo.bar_count() == 0


# ======================================================================
# D2b 带 symbols 的增量不推进整市场水位
# ======================================================================

def test_fetcher_incremental_with_symbols_keeps_watermark(tmp_path):
    db, config, repo, whitelist, fetcher = build_fetcher(tmp_path)
    result = fetcher.run(
        FakeContext("t-sym"),
        {"market": MARKET, "mode": "incremental", "end": "2025-01-06", "symbols": ["000001.SZ"]},
    )
    assert result["dates_done"] == 3
    assert result["dates_failed"] == 0
    # 只抓指定标的，但不得推进整市场水位（其他白名单成员这段历史仍需覆盖）
    assert result["watermark_advanced"] is False
    assert repo.sync_state_watermark(MARKET) is None
    assert repo.db.scalar("SELECT COUNT(DISTINCT symbol) FROM bar_daily") == 1


# ======================================================================
# akshare 单标的失败 → 写入任务结果 failed_symbols（水位仍推进）
# ======================================================================

def test_fetcher_partial_symbol_failure_reported_in_result(tmp_path, monkeypatch):
    db, config, repo, whitelist, fetcher = build_fetcher(tmp_path)
    source = fetcher.source
    real_daily = source.get_daily

    def partial_fail(market, trade_date, symbols=None):
        df = real_daily(market, trade_date, symbols=symbols)
        temp = df[df["ts_code"] != "600519.SH"].copy()  # 600519 接口失败被跳过
        temp.attrs["failed_symbols"] = [{"symbol": "600519.SH", "error": "RuntimeError: 接口异常"}]
        return temp

    monkeypatch.setattr(source, "get_daily", partial_fail)
    result = fetcher.run(FakeContext("t-part"), {"market": MARKET, "mode": "incremental", "end": "2025-01-06"})
    # 部分成功：3 天完成、水位仍推进（避免退市标卡死整市场）
    assert result["dates_done"] == 3
    assert result["dates_failed"] == 0
    assert result["watermark_advanced"] is True
    # 失败标的写进任务结果（修复前只写模块级日志，失败缺失静默永久化）
    assert result["failed_symbols"] == [{"symbol": "600519.SH", "error": "RuntimeError: 接口异常"}]
    # 成功标的全量入库：3 天 × 2 只；失败标的无数据
    assert repo.bar_count() == 6
    assert repo.db.scalar("SELECT COUNT(*) FROM bar_daily WHERE symbol='600519.SH'") == 0


# ======================================================================
# D2c 增量未收盘时截止昨日（market_closed 按市场本地日期+时间判定，跨日安全）
# ======================================================================

def test_market_closed_boundary():
    # 用户反例：UTC 1/5 22:00 对 CN_A 是北京 1/6 06:00（盘前）——只看 UTC 小时会误判为已收盘
    assert market_closed(MARKET_CN_A, datetime(2026, 1, 5, 22, 0)) is False
    assert market_closed(MARKET_CN_A, datetime(2026, 1, 6, 0, 0)) is False  # 北京 1/6 08:00 盘前
    # 北京 1/6 15:00 收盘 = UTC 1/5 07:00
    assert market_closed(MARKET_CN_A, datetime(2026, 1, 5, 7, 0)) is True
    assert market_closed(MARKET_CN_A, datetime(2026, 1, 5, 6, 59)) is False
    # HK 16:00 = UTC 08:00（香港 1/6 16:00）
    assert market_closed(MARKET_HK, datetime(2026, 1, 5, 8, 0)) is True
    assert market_closed(MARKET_HK, datetime(2026, 1, 5, 7, 59)) is False
    # US 冬令时 16:00 ET = UTC 21:00（美东 1/5 16:00）
    assert market_closed("US", datetime(2026, 1, 5, 21, 0)) is True
    assert market_closed("US", datetime(2026, 1, 5, 20, 59)) is False
    # US 夏令时 16:00 ET = UTC 20:00
    assert market_closed("US", datetime(2026, 7, 5, 20, 0)) is True
    assert market_closed("US", datetime(2026, 7, 5, 19, 59)) is False


def test_market_local_date_crossday():
    """市场本地日期随 UTC 偏移进位/退位：US 不依赖服务器本地日期。"""
    assert market_local_date(MARKET_CN_A, datetime(2026, 1, 5, 22, 0)) == "2026-01-06"
    assert market_local_date(MARKET_HK, datetime(2026, 1, 5, 15, 59)) == "2026-01-05"
    # 冬令时：UTC 1/5 19:00 = 美东 1/5 14:00（市场本地日期仍是 1/5，不是服务器 tomorrow）
    assert market_local_date("US", datetime(2026, 1, 5, 19, 0)) == "2026-01-05"
    # 夏令时：UTC 7/6 03:00 = 美东 7/5 23:00（市场本地日期比 UTC 退一天）
    assert market_local_date("US", datetime(2026, 7, 6, 3, 0)) == "2026-07-05"
    assert market_local_datetime(MARKET_CN_A, datetime(2026, 1, 5, 22, 0)).time().hour == 6
