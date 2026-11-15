from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_db

from yafip.api import routes_whitelist
from yafip.api.responses import install_error_handlers
from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.data.repository import Repository
from yafip.data.whitelist import WhitelistService


class FakeData:
    def __init__(self):
        self.calls = 0

    def submit(self, task_type, executor, market, params=None, source="manual"):
        self.calls += 1
        assert task_type == "whitelist.update"
        return "task-123"


def make_client(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    service = WhitelistService(db, repo, config, AuditService(db))
    data = FakeData()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_whitelist.router)
    app.state.ctx = SimpleNamespace(repo=repo, whitelist=service, data=data)
    return TestClient(app), db, repo, config, data


def test_config_api(tmp_path):
    client, db, repo, config, _ = make_client(tmp_path)

    view = client.get("/api/whitelist/config").json()["data"]
    assert "enabled" not in view  # 兼容字段已下线
    assert view["metric"] == "total_mv"
    assert view["threshold_yi"] == 50.0

    updated = client.put(
        "/api/whitelist/config",
        json={"threshold_yi": 60},
    )
    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["threshold_wan"] == 600000.0
    assert config.get("whitelist.threshold_wan") == "600000.0"
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='whitelist.config'") == 1


def test_recompute_api_manual_trigger(tmp_path):
    client, db, repo, config, data = make_client(tmp_path)

    response = client.post("/api/whitelist/recompute", params={"market": "CN_A"})
    assert response.status_code == 202
    assert response.json()["data"]["task_id"] == "task-123"
    assert data.calls == 1


def test_member_crud_api(tmp_path):
    client, db, repo, _, _ = make_client(tmp_path)

    response = client.post(
        "/api/whitelist/members",
        json={"market": "CN_A", "symbol": "600519.SH", "reason": "手动维护"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["symbol"] == "600519.SH"

    conflict = client.post(
        "/api/whitelist/members",
        json={"market": "CN_A", "symbol": "600519.sh"},
    )
    assert conflict.status_code == 409
    assert "已存在" in conflict.json()["message"]

    listed = client.get(
        "/api/whitelist/members", params={"market": "CN_A", "keyword": "600519"}
    ).json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["reason"] == "手动维护"

    updated = client.put(
        "/api/whitelist/members/600519.SH",
        json={"reason": ""},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["reason"] is None

    deleted = client.delete("/api/whitelist/members/600519.SH")
    assert deleted.status_code == 200
    assert repo.member_count() == 0
    assert db.scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'whitelist.member_%'"
    ) == 3


def test_member_api_validation_and_missing(tmp_path):
    client, _, _, _, _ = make_client(tmp_path)

    invalid = client.post("/api/whitelist/members", json={"market": "CN_A", "symbol": "600519"})
    assert invalid.status_code == 400
    assert invalid.json()["code"] == 5001

    missing_update = client.put(
        "/api/whitelist/members/000002.SZ", json={"reason": "x"}
    )
    assert missing_update.status_code == 404

    missing_delete = client.delete("/api/whitelist/members/000002.SZ")
    assert missing_delete.status_code == 404
