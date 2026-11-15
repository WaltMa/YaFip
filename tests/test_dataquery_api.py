from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_db, seed_calendar

from yafip.api import routes_dataquery
from yafip.api.responses import install_error_handlers
from yafip.config import ConfigService
from yafip.data.market import MARKET_CN_A
from yafip.data.repository import Repository


def make_client(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    seed_calendar(db, MARKET_CN_A)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_dataquery.router)
    app.state.ctx = SimpleNamespace(repo=repo, config=config, whitelist=SimpleNamespace(state=lambda m: set()))
    return TestClient(app), db, repo


def test_instruments_api(tmp_path):
    client, db, repo = make_client(tmp_path)
    repo.upsert_instrument(
        MARKET_CN_A,
        [
            {"symbol": "600519.SH", "name": "贵州茅台", "asset_type": "STOCK", "status": "L"},
            {"symbol": "513100.SH", "name": "纳指ETF", "asset_type": "ETF", "status": "L"},
        ],
        source="tushare",
    )
    payload = client.get("/api/data/instruments", params={"market": "CN_A"}).json()["data"]
    assert payload["total"] == 2

    filtered = client.get("/api/data/instruments", params={"asset_type": "ETF"}).json()["data"]
    assert filtered["total"] == 1
    assert filtered["items"][0]["symbol"] == "513100.SH"


def test_daily_api_requires_symbol(tmp_path):
    client, _, _ = make_client(tmp_path)
    missing = client.get("/api/data/daily")
    assert missing.status_code == 400
    assert missing.json()["code"] == 4001


def test_progress_watermark_view(tmp_path):
    client, db, repo = make_client(tmp_path)
    repo.set_sync_state(MARKET_CN_A, "2026-11-13", status="ok")
    payload = client.get("/api/data/progress", params={"market": "CN_A"}).json()["data"]
    assert payload["watermark"] == "2026-11-13"
    assert payload["whitelist_size"] == 0
    assert "latest_bar_date" in payload
