from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_db

from yafip.api import routes_datasource
from yafip.api.responses import install_error_handlers
from yafip.config import ConfigError, ConfigService
from yafip.core.audit import AuditService
from yafip.data.base import DataSourceError
from yafip.data.repository import Repository
from yafip.data.tushare_source import TushareSource


class FakeSource:
    name = "tushare"

    def test_connection(self):
        return {"ok": True, "steps": [{"name": "trade_cal", "ok": True, "message": "最近交易日 2026-11-13"}]}


def make_client(db):
    repo = Repository(db)
    config = ConfigService(db)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_datasource.router)
    app.state.ctx = SimpleNamespace(
        repo=repo,
        audit=AuditService(db),
        source=FakeSource(),
        config=config,
    )
    return TestClient(app), repo


def test_list_masks_token_and_includes_meta(tmp_path):
    db = make_db(tmp_path)
    client, repo = make_client(db)
    repo.save_datasource("tushare", token="1234567890abcdef", rpm_limit=30, enabled=True)

    payload = client.get("/api/datasources").json()
    assert payload["code"] == 0
    items = payload["data"]["items"]
    assert len(items) == 2  # tushare + akshare

    item = items[0]  # tushare 排前
    assert item["name"] == "tushare"
    assert item["title"] == "tushare pro"
    assert item["token_masked"] == "123****cdef"
    assert item["has_token"] is True
    assert item["rpm_limit"] == 30
    assert item["enabled"] is True
    assert "token" not in item

    ak = items[1]
    assert ak["name"] == "akshare"
    assert ak["requires_token"] is False


def test_save_and_clear_optional_fields(tmp_path):
    db = make_db(tmp_path)
    client, repo = make_client(db)

    response = client.put(
        "/api/datasources/tushare",
        json={"token": "token-123456", "api_url": "http://gateway.local", "rpm_limit": 60, "enabled": True},
    )
    assert response.status_code == 200
    row = repo.get_datasource("tushare")
    assert row["token"] == "token-123456"
    assert row["api_url"] == "http://gateway.local"
    assert row["rpm_limit"] == 60

    response = client.put(
        "/api/datasources/tushare",
        json={"token": "", "api_url": "", "rpm_limit": 0, "enabled": False},
    )
    assert response.status_code == 200
    row = repo.get_datasource("tushare")
    assert row["token"] == "token-123456"  # 留空不修改 token
    assert row["api_url"] is None
    assert row["rpm_limit"] is None
    assert row["enabled"] == 0


def test_unknown_source_returns_404(tmp_path):
    db = make_db(tmp_path)
    client, _ = make_client(db)
    response = client.get("/api/datasources/unknown")
    assert response.status_code == 404
    assert response.json()["code"] == 1002


def test_test_connection_requires_token(tmp_path):
    db = make_db(tmp_path)
    client, _ = make_client(db)
    response = client.post("/api/datasources/tushare/test")
    assert response.status_code == 400
    assert response.json()["code"] == 1003


def test_test_connection_success_and_audit(tmp_path):
    db = make_db(tmp_path)
    client, repo = make_client(db)
    repo.save_datasource("tushare", token="token-123456")

    response = client.post("/api/datasources/tushare/test")
    assert response.status_code == 200
    assert response.json()["data"]["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='datasource.test'") == 1


def test_tushare_source_disabled(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    repo.save_datasource("tushare", token="token-123456", enabled=False)
    source = TushareSource(repo, ConfigService(db))
    with pytest.raises(DataSourceError):
        source._pro()


def test_datasource_token_is_db_only(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    repo = Repository(db)
    repo.save_datasource("tushare", token="db-token")
    monkeypatch.setenv("YAFIP_TUSHARE_TOKEN", "env-token")
    assert repo.datasource_token() == "db-token"


def test_tushare_source_custom_api_url(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    repo.save_datasource("tushare", token="token-123456", api_url="http://127.0.0.1:9999/gateway")
    source = TushareSource(repo, ConfigService(db))
    client = source._pro()
    assert client._DataApi__http_url == "http://127.0.0.1:9999/gateway"


def test_datasource_keys_not_exposed_via_config(tmp_path):
    """token 等数据源配置并入 app_config 后，不得经 /api/config 视图外泄。"""
    db = make_db(tmp_path)
    repo = Repository(db)
    repo.save_datasource("tushare", token="secret-token-123456", enabled=True)

    config = ConfigService(db)
    exposed = [k for k in config.all() if k.startswith("datasource.")]
    assert exposed == []

    with pytest.raises(ConfigError):
        config.set_many({"datasource.tushare.token": "evil-token"})
    # 被拒绝的写入不应落库
    assert repo.datasource_token() == "secret-token-123456"
