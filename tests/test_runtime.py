from __future__ import annotations

import os

import pytest

from yafip import runtime
from yafip.config import ConfigService
from yafip.core.db import Database, now_iso


def make_root_db(root):
    db = Database(root / "data" / "yafip.db")
    db.init_schema()
    config = ConfigService(db)
    config.seed_defaults()
    return db, config


def test_default_port_is_8203(tmp_path):
    db, config = make_root_db(tmp_path)
    assert config.get("server.port") == "8203"
    assert runtime.resolve_port(tmp_path) == 8203


def test_legacy_port_is_migrated(tmp_path):
    db = Database(tmp_path / "data" / "yafip.db")
    db.init_schema()
    db.execute(
        "INSERT INTO app_config(key, value, updated_at) VALUES('server.port','8765',?)",
        (now_iso(),),
    )
    config = ConfigService(db)
    config.seed_defaults()
    assert config.get("server.port") == "8203"


def test_port_migration_is_one_time(tmp_path):
    db, config = make_root_db(tmp_path)
    config.set_many({"server.port": "8765"})
    config.seed_defaults()
    assert config.get("server.port") == "8765"
    assert "_migration.server_port_8765" not in config.all()


def test_set_many_rejects_invalid_values(tmp_path):
    """值域校验：0 心跳/非法端口/负数并发等必须被拒绝，避免 SSE 死循环与不可用状态。"""
    db, config = make_root_db(tmp_path)
    from yafip.config import ConfigError

    cases = {
        "sse.heartbeat_sec": "0",          # 0 → SSE wait_for 立即超时，CPU 打满
        "sse.heartbeat_sec": "-5",
        "server.port": "abc",
        "server.port": "0",
        "server.port": "70000",
        "task.max_workers": "-1",
        "task.max_workers": "0",
        "fetch.rpm": "0",
        "fetch.meta_rpm": "-3",
        "data.start_date": "2025/01/01",
        "data.include_delisted": "maybe",
        "whitelist.threshold_wan": "-1.0",
    }
    for key, value in cases.items():
        with pytest.raises(ConfigError):
            config.set_many({key: value})
        # 失败必须是原子：未落库、缓存未变
        assert config.get(key) != value or config.get(key) is None
    config.set_many({"sse.heartbeat_sec": "1", "fetch.rpm": "1"})
    assert config.get_int("sse.heartbeat_sec") == 1


def test_env_port_overrides_config(tmp_path, monkeypatch):
    make_root_db(tmp_path)
    monkeypatch.setenv("YAFIP_PORT", "9123")
    assert runtime.resolve_port(tmp_path) == 9123


def test_load_env_file_and_real_env_wins(tmp_path):
    key = "YAFIP_TEST_FLAG"
    original = os.environ.get(key)
    (tmp_path / ".env").write_text(f"{key}=from_file\n", encoding="utf-8")
    try:
        os.environ.pop(key, None)
        runtime.load_env(tmp_path)
        assert os.environ[key] == "from_file"

        os.environ[key] = "from_env"
        runtime.load_env(tmp_path)
        assert os.environ[key] == "from_env"
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def test_run_info_roundtrip(tmp_path):
    runtime.write_run_info(tmp_path, 8203)
    info = runtime.read_run_info(tmp_path)
    assert info["pid"] == os.getpid()
    assert info["port"] == 8203
    assert info["url"] == "http://127.0.0.1:8203"

    runtime.clear_run_info(tmp_path, os.getpid())
    assert runtime.read_run_info(tmp_path) == {}
