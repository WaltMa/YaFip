from __future__ import annotations

import threading
import time

from conftest import make_db

from yafip.config import ConfigService
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter, TaskSpec


def wait_for(tasks, task_id, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = tasks.get(task_id)
        if record["status"] in statuses:
            return record
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} not in {statuses}: {tasks.get(task_id)['status']}")


def _spec(task_type, market, handler, lock_key):
    return TaskSpec(
        task_type=task_type, executor="fake." + task_type, market=market,
        source="manual", params={}, handler=handler, lock_key=lock_key,
    )


def test_concurrency_cap_queues_extra_task(tmp_path):
    """并发上限生效：超限任务保持 PENDING（排队），不占用工作线程。"""
    db = make_db(tmp_path)
    config = ConfigService(db)
    config.set_many({"task.max_concurrency": "2"})
    center = TaskCenter(db, SSEBroker(), config=config)

    events = {"a": threading.Event(), "b": threading.Event()}

    def hold(name):
        def handler(ctx, params):
            events[name].wait(10)
            return {"held": name}
        return handler

    center.register("hold_a", hold("a"))
    center.register("hold_b", hold("b"))
    center.register("hold_c", lambda ctx, params: {"c": True})

    a = center.submit(_spec("t.a", "CN_A", "hold_a", "t.a:CN_A"))
    b = center.submit(_spec("t.b", "CN_A", "hold_b", "t.b:CN_A"))
    wait_for(center, a, {"RUNNING"})
    wait_for(center, b, {"RUNNING"})

    # 第三个任务：并发已达上限 → PENDING 排队
    c = center.submit(_spec("t.c", "CN_A", "hold_c", "t.c:CN_A"))
    assert center.get(c)["status"] == "PENDING"
    with center._lock:
        assert c in center._waiting
    # 排队任务未占用工作线程：active 运行数仍是 2
    with center._lock:
        assert center._active == 2

    # 释放一个槽位 → 排队任务自动启动
    events["a"].set()
    wait_for(center, a, {"SUCCESS"})
    wait_for(center, c, {"SUCCESS"}, timeout=5.0)
    with center._lock:
        assert c not in center._waiting

    events["b"].set()
    wait_for(center, b, {"SUCCESS"})
    assert center.active_count() == 0


def test_waiting_task_can_be_killed(tmp_path):
    """排队任务被杀：不占用槽位、不复活执行。"""
    db = make_db(tmp_path)
    config = ConfigService(db)
    config.set_many({"task.max_concurrency": "1"})
    center = TaskCenter(db, SSEBroker(), config=config)
    release = threading.Event()

    def hold(ctx, params):
        release.wait(10)
        return {}

    center.register("hold", hold)
    center.register("quick", lambda ctx, params: {"ok": True})

    first = center.submit(_spec("t.first", "CN_A", "hold", "t.first:CN_A"))
    wait_for(center, first, {"RUNNING"})
    second = center.submit(_spec("t.second", "CN_A", "quick", "t.second:CN_A"))
    assert center.get(second)["status"] == "PENDING"

    center.kill(second)
    assert center.get(second)["status"] == "KILLED"

    release.set()
    wait_for(center, first, {"SUCCESS"})
    # 释放槽位后不复活已杀死的排队任务
    time.sleep(0.2)
    assert center.get(second)["status"] == "KILLED"
    assert center.active_count() == 0
