from __future__ import annotations

import sqlite3

import pytest

from yafip import bootstrap
from yafip.config import ConfigService
from yafip.core.db import Database, SchemaVersionError


def open_db(root) -> Database:
    return Database(root / "data" / "yafip.db")


def test_first_run_creates_fresh_empty_whitelist(tmp_path):
    result = bootstrap.initialize(tmp_path)
    assert result.status == "created"
    assert result.count == 0
    assert "全新 schema" in result.message

    db = open_db(tmp_path)
    try:
        assert db.scalar("PRAGMA user_version") == 10
        assert db.scalar("SELECT COUNT(*) FROM whitelist_member") == 0
        assert db.scalar("SELECT COUNT(*) FROM bar_daily") == 0
        config = ConfigService(db)
        assert config.get("whitelist.metric") == "total_mv"
        assert config.get("whitelist.threshold_wan") == "500000.0"
    finally:
        db.close_all()


def test_second_run_reports_empty_whitelist(tmp_path):
    assert bootstrap.initialize(tmp_path).status == "created"
    second = bootstrap.initialize(tmp_path)
    assert second.status == "empty"
    assert second.count == 0


def test_old_db_is_rejected_with_archive_hint(tmp_path):
    """旧库（user_version<10 且有业务表）必须报错并提示归档。"""
    db_path = tmp_path / "data" / "yafip.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE app_config(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE stock_basic(ts_code TEXT PRIMARY KEY, name TEXT);
            PRAGMA user_version = 8;
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SchemaVersionError, match="归档"):
        bootstrap.initialize(tmp_path)


def test_cli_prints_first_run_message_to_stdout_and_log(tmp_path, monkeypatch, capsys):
    from yafip import cli

    result = bootstrap.InitResult(
        "created",
        0,
        "首次创建数据库：全新 schema（user_version=10），白名单为空",
    )
    monkeypatch.setattr(bootstrap, "initialize", lambda root: result)

    cli._ensure_whitelist(tmp_path)

    assert "[yafip] 首次创建数据库" in capsys.readouterr().out
    log_text = (tmp_path / "logs" / "start_yafip.log").read_text(encoding="utf-8")
    assert "首次创建数据库" in log_text
