from __future__ import annotations

import time
from types import SimpleNamespace

from conftest import make_db

from yafip.core.lifecycle import GracefulStop
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter, TaskSpec


class FakeConfig:
    def get_int(self, key, default=0):
        return 0


class FakeServer:
    def __init__(self):
        self.should_exit = False


def test_stop_waits_for_running_task(tmp_path):
    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=1)
    server = FakeServer()

    def slow(ctx, params):
        for _ in range(30):
            ctx.checkpoint()
            time.sleep(0.02)
        return {"done": True}

    center.register("slow", slow)
    task_id = center.submit(
        TaskSpec(task_type="demo", executor="fake.demo", market=None, source="manual",
                 params={}, handler="slow", lock_key="demo")
    )

    app = SimpleNamespace(
        state=SimpleNamespace(
            ctx=SimpleNamespace(
                tasks=center,
                config=FakeConfig(),
                root=tmp_path,
            ),
            server=server,
        )
    )
    controller = GracefulStop(app)

    assert controller.request() is True
    deadline = time.time() + 5
    while not server.should_exit and time.time() < deadline:
        time.sleep(0.02)

    assert server.should_exit is True
    assert center.get(task_id)["status"] == "SUCCESS"
    assert center.active_count() == 0


def test_stop_is_idempotent_and_rejects_new_tasks(tmp_path):
    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=1)
    server = FakeServer()
    app = SimpleNamespace(
        state=SimpleNamespace(
            ctx=SimpleNamespace(
                tasks=center,
                config=FakeConfig(),
                root=tmp_path,
            ),
            server=server,
        )
    )
    controller = GracefulStop(app)
    assert controller.request() is True
    assert controller.request() is False

    import pytest

    from yafip.core.task_center import TaskError

    center.begin_drain()
    with pytest.raises(TaskError) as exc:
        center.submit(
            TaskSpec(task_type="demo", executor="fake.demo", market=None, source="manual",
                     params={}, handler="missing", lock_key="demo")
        )
    assert "停止" in str(exc.value)
