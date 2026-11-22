from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
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
        json={"token": "token-123456", "api_url": "https://gateway.local", "rpm_limit": 60, "enabled": True},
    )
    assert response.status_code == 200
    row = repo.get_datasource("tushare")
    assert row["token"] == "token-123456"
    assert row["api_url"] == "https://gateway.local"
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


def test_api_url_restricted_to_https_or_localhost(tmp_path):
    """api_url 白名单：明文 http 到远程主机拒绝（token 防窃取），https/本机放行。"""
    db = make_db(tmp_path)
    client, _ = make_client(db)

    def put(api_url):
        return client.put(
            "/api/datasources/tushare",
            json={"token": "token-123456", "api_url": api_url},
        )

    assert put("http://evil.example/gateway").status_code == 400
    assert put("http://192.168.1.5/gateway").status_code == 400
    assert put("https://api.tushare.pro").status_code == 200
    assert put("https://gateway.example.com/custom").status_code == 200
    assert put("http://127.0.0.1:9999/gateway").status_code == 200
    assert put("").status_code == 200
    assert put(None).status_code == 200


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


def test_tushare_test_connection_uses_single_light_interface(tmp_path, monkeypatch):
    """连接测试只调一次 daily（120 积分即可用），不连带 trade_cal/daily_basic 等高积分接口。"""
    db = make_db(tmp_path)
    repo = Repository(db)
    repo.save_datasource("tushare", token="token-123456")
    source = TushareSource(repo, ConfigService(db))

    calls: list[str] = []

    class FakePro:
        def daily(self, **kwargs):
            calls.append("daily")
            assert kwargs.get("ts_code") == "000001.SZ"
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "trade_date": ["2024-01-02", "2024-01-03"],
                    "close": [9.5, 9.6],
                }
            )

        def index_basic(self, **_kwargs):
            raise AssertionError("测试连接不应调用 index_basic")

        def trade_cal(self, **_kwargs):
            raise AssertionError("测试连接不应调用 trade_cal")

        def daily_basic(self, **_kwargs):
            raise AssertionError("测试连接不应调用 daily_basic")

    monkeypatch.setattr(source, "_pro", lambda **_kwargs: FakePro())
    result = source.test_connection()

    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == ["daily"]
    assert calls == ["daily"]
    assert repo.get_datasource("tushare")["last_test_ok"] == 1


def test_tushare_stock_list_single_call_l_status(tmp_path, monkeypatch):
    """元数据标的列表只调 1 次 stock_basic（list_status='L'），不拉 D/P。

    低积分账号 stock_basic 常被限到 1 次/小时，按 L/D/P 分 3 段拉取在单个任务内
    无法完成（第一次成功后第二次即被拒，且前序结果不落库，重试永远卡住）。
    """
    db = make_db(tmp_path)
    repo = Repository(db)
    repo.save_datasource("tushare", token="token-123456")
    source = TushareSource(repo, ConfigService(db))

    calls: list[dict] = []

    class FakePro:
        def stock_basic(self, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "600519.SH"],
                    "symbol": ["000001", "600519"],
                    "name": ["平安银行", "贵州茅台"],
                    "area": ["深圳", "贵州"],
                    "industry": ["银行", "白酒"],
                    "market": ["主板", "主板"],
                    "list_date": ["1991-04-03", "2001-08-27"],
                    "delist_date": [None, None],
                }
            )

    monkeypatch.setattr(source, "_pro", lambda **_kwargs: FakePro())
    df = source.get_stock_list("CN_A")

    assert len(calls) == 1
    assert calls[0]["list_status"] == "L"
    assert set(df["list_status"]) == {"L"}
    assert len(df) == 2


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
