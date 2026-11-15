from __future__ import annotations

import pytest

from yafip.core.db import Database, SchemaVersionError
from yafip.data.migrations import MIGRATIONS, apply_migrations

# 新库应有的业务表（全模型）
EXPECTED_TABLES = {
    "app_config",
    "audit_log",
    "instrument",
    "instrument_metric",
    "bar_daily",
    "bar_adj_factor",
    "trade_calendar",
    "whitelist_member",
    "sync_state",
    "staging_batch",
    "task",
    "task_log",
    "data_quality_issue",
}
# 明确不存在的旧表
ABSENT_TABLES = {
    "stock_basic",
    "stock_daily",
    "stock_adj_factor",
    "fetch_progress",
    "datasource_config",
    "schedule",
}


def _tables(db: Database) -> set[str]:
    return {
        str(r["name"])
        for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(r["name"]).startswith("sqlite_")
    }


def test_init_schema_fresh_db_creates_full_model(tmp_path):
    db = Database(tmp_path / "data" / "yafip.db")
    try:
        first_run = db.init_schema()
        assert first_run is True
        tables = _tables(db)
        assert EXPECTED_TABLES <= tables
        assert not (ABSENT_TABLES & tables)
        assert db.scalar("PRAGMA user_version") == 10
        # bar 表索引与质量表自然键
        daily_cols = {row[1] for row in db.query("PRAGMA table_info(bar_daily)")}
        assert {"market", "symbol", "trade_date", "source"} <= daily_cols
        quality_cols = {row[1] for row in db.query("PRAGMA table_info(data_quality_issue)")}
        assert {"market", "symbol"} <= quality_cols
        task_cols = {row[1] for row in db.query("PRAGMA table_info(task)")}
        assert {"task_type", "executor", "market"} <= task_cols
        assert not ({"business", "action"} & task_cols)
    finally:
        db.close_all()


def test_init_schema_idempotent_on_restart(tmp_path):
    db = Database(tmp_path / "data" / "yafip.db")
    try:
        db.init_schema()
        first_run = db.init_schema()  # 重复启动：幂等，不抛异常
        assert first_run is False
        assert db.scalar("PRAGMA user_version") == 10
        assert EXPECTED_TABLES <= _tables(db)
    finally:
        db.close_all()


def test_init_schema_rejects_old_db(tmp_path):
    """旧库（user_version<10 且有业务表）必须明确报错并提示归档。"""
    db = Database(tmp_path / "data" / "yafip.db")
    try:
        with db.tx() as conn:
            conn.execute(
                "CREATE TABLE app_config(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute("CREATE TABLE stock_basic(ts_code TEXT PRIMARY KEY, name TEXT)")
            conn.execute("PRAGMA user_version = 8")
        with pytest.raises(SchemaVersionError, match="归档"):
            db.init_schema()
    finally:
        db.close_all()


def test_init_schema_accepts_current_version_db(tmp_path):
    """user_version=10 的库正常启动（即使文件已存在）。"""
    db = Database(tmp_path / "data" / "yafip.db")
    try:
        db.init_schema()
        db.init_schema()  # 第二次：user_version 已是 10，不触发守卫
        assert db.scalar("PRAGMA user_version") == 10
    finally:
        db.close_all()


def test_apply_migrations_idempotent(tmp_path):
    db = Database(tmp_path / "data" / "yafip.db")
    try:
        db.init_schema()
        applied = apply_migrations(db, 10)
        assert applied == 0
        # 重复应用不抛异常
        apply_migrations(db, 10)
        assert db.scalar("PRAGMA user_version") == 10
    finally:
        db.close_all()


def test_migrations_registry_sorted_and_unique(tmp_path):
    versions = [v for v, _name, _fn in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
