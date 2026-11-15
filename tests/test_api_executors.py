from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_db

from yafip.api import routes_executors, routes_fetch
from yafip.api.responses import install_error_handlers
from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter
from yafip.data.executors import build_default_registry
from yafip.data.service import DataService


def make_client(tmp_path):
    db = make_db(tmp_path)
    config = ConfigService(db)
    registry = build_default_registry()
    center = TaskCenter(db, SSEBroker())
    # 任务 handler 由  Task 3 提供；此处注册桩以打通提交链路
    for name in (
        "tasks.meta_sync_all", "tasks.meta_sync_symbols", "tasks.whitelist_update",
        "tasks.bars_incremental", "tasks.bars_range", "data.quality",
    ):
        center.register(name, lambda ctx, params: {"ok": True})
    data = DataService(center, registry, config)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_executors.router)
    app.include_router(routes_fetch.router)
    app.state.ctx = SimpleNamespace(
        data=data, executors=registry, audit=AuditService(db),
    )
    return TestClient(app), center, db


def _wait_status(client, center, task_id, status="SUCCESS", timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = center.get(task_id)
        if record["status"] in (status, "FAILED", "KILLED"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} not finished: {center.get(task_id)}")


def test_list_executors_for_cn_a(tmp_path):
    client, _, _ = make_client(tmp_path)
    payload = client.get("/api/executors", params={"market": "CN_A"}).json()["data"]
    names = [item["name"] for item in payload["items"]]
    assert names == [
        "tushare.bars.incremental",
        "tushare.bars.range",
        "tushare.meta.sync_all",
        "tushare.meta.sync_symbols",
        "tushare.whitelist.update",
    ]
    for item in payload["items"]:
        assert item["implemented"] is True
        assert "params_schema" in item
        assert item["task_type_label"]
    range_item = next(i for i in payload["items"] if i["task_type"] == "bars.range")
    assert set(range_item["params_schema"]) == {"start", "end", "symbols"}


def test_list_executors_hk_returns_akshare(tmp_path):
    client, _, _ = make_client(tmp_path)
    payload = client.get("/api/executors", params={"market": "HK"}).json()["data"]
    names = [item["name"] for item in payload["items"]]
    assert names == [
        "akshare.bars.incremental",
        "akshare.bars.range",
        "akshare.meta.sync_all",
    ]


def test_post_tasks_valid_submission(tmp_path):
    client, center, db = make_client(tmp_path)
    resp = client.post(
        "/api/tasks",
        json={
            "task_type": "bars.range",
            "executor": "tushare.bars.range",
            "market": "CN_A",
            "params": {"start": "2025-01-01", "end": "2025-01-31"},
        },
    )
    assert resp.status_code == 202
    task_id = resp.json()["data"]["task_id"]
    record = center.get(task_id)
    assert record["task_type"] == "bars.range"
    assert record["executor"] == "tushare.bars.range"
    assert record["market"] == "CN_A"
    assert record["params"].find("2025-01-01") > 0
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='executor.submit'") == 1


def test_post_tasks_rejects_invalid(tmp_path):
    client, center, _ = make_client(tmp_path)
    # 串类型
    resp = client.post(
        "/api/tasks",
        json={"task_type": "bars.incremental", "executor": "tushare.bars.range", "market": "CN_A", "params": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3001
    # 缺必填参数
    resp = client.post(
        "/api/tasks",
        json={"task_type": "bars.range", "executor": "tushare.bars.range", "market": "CN_A", "params": {}},
    )
    assert resp.status_code == 400
    # 非法 symbol
    resp = client.post(
        "/api/tasks",
        json={
            "task_type": "meta.sync_symbols",
            "executor": "tushare.meta.sync_symbols",
            "market": "CN_A",
            "params": {"symbols": ["bad"]},
        },
    )
    assert resp.status_code == 400
    # 均不创建任务记录
    items, total = center.list()
    assert items == [] and total == 0


def test_quality_scan_route_still_works(tmp_path):
    client, center, _ = make_client(tmp_path)
    resp = client.post(
        "/api/data/quality-scan",
        json={"market": "CN_A", "start": "2025-01-01", "end": "2025-12-31"},
    )
    assert resp.status_code == 202
    task_id = resp.json()["data"]["task_id"]
    record = center.get(task_id)
    assert record["task_type"] == "quality.scan"
    assert record["executor"] == "tushare.quality.scan"
    assert record["market"] == "CN_A"
