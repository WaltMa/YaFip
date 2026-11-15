from __future__ import annotations

import time

import pytest

from conftest import make_db

from yafip.core import task_center as tc
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter, TaskConflict, TaskSpec


def wait_for(tasks, task_id, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = tasks.get(task_id)
        if record["status"] in statuses:
            return record
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未进入 {statuses}，当前 {tasks.get(task_id)['status']}")


def make_center(tmp_path):
    db = make_db(tmp_path)
    center = TaskCenter(db, SSEBroker(), max_workers=2)
    return db, center


def spec(handler, task_type="demo", market=None, lock_key=None):
    return TaskSpec(
        task_type=task_type,
        executor=f"fake.{task_type}",
        market=market,
        source="manual",
        params={},
        handler=handler,
        lock_key=lock_key or (task_type if market is None else f"{task_type}:{market}"),
    )


def test_success_and_retry(tmp_path):
    db, center = make_center(tmp_path)
    center.register("ok", lambda ctx, params: {"value": 1})
    task_id = center.submit(spec("ok"))
    record = wait_for(center, task_id, {"SUCCESS"})
    assert record["result"] == '{"value": 1}'
    assert record["task_type"] == "demo"
    assert record["executor"] == "fake.demo"

    new_id = center.retry(task_id)
    assert new_id != task_id
    new_record = wait_for(center, new_id, {"SUCCESS"})
    assert new_record["retry_of"] == task_id


def test_task_conflict(tmp_path):
    db, center = make_center(tmp_path)

    def slow(ctx, params):
        for _ in range(40):
            ctx.checkpoint()
            time.sleep(0.01)
        return {}

    center.register("slow", slow)
    first = center.submit(spec("slow", lock_key="bars:CN_A"))
    with pytest.raises(TaskConflict):
        center.submit(spec("slow", lock_key="bars:CN_A"))
    # 不同 lock_key 可并行
    other = center.submit(spec("slow", lock_key="bars:HK"))
    wait_for(center, other, {"SUCCESS"})
    wait_for(center, first, {"SUCCESS"})


def test_kill_running_task_is_cooperative(tmp_path):
    db, center = make_center(tmp_path)

    def slow(ctx, params):
        for _ in range(200):
            ctx.checkpoint()
            time.sleep(0.01)
        return {}

    center.register("slow", slow)
    task_id = center.submit(spec("slow"))
    wait_for(center, task_id, {"RUNNING"})
    center.kill(task_id)
    record = wait_for(center, task_id, {"KILLED"}, timeout=5.0)
    assert "取消" in (record.get("message") or "")


def test_kill_pending_task(tmp_path):
    db, center = make_center(tmp_path)

    def slow(ctx, params):
        time.sleep(0.5)
        return {}

    center.register("slow", slow)
    ids = [
        center.submit(spec("slow", lock_key="demo-%s" % index))
        for index in range(2)
    ]
    pending = center.submit(spec("slow", task_type="demo2", lock_key="demo2"))
    record = center.get(pending)
    if record["status"] == "PENDING":
        center.kill(pending)
        assert center.get(pending)["status"] == "KILLED"
    else:
        center.kill(pending)
    for task_id in ids + [pending]:
        wait_for(center, task_id, {"SUCCESS", "KILLED"}, timeout=5.0)


def test_recover_marks_running_as_failed(tmp_path):
    db, center = make_center(tmp_path)
    db.execute(
        """
        INSERT INTO task(id, task_type, executor, market, source, lock_key, params, handler, status, progress, created_at)
        VALUES('abc','demo','fake.demo',NULL,'manual','demo','{}','slow','RUNNING',10,'2026-01-01T00:00:00')
        """
    )
    assert center.recover() == 1
    assert center.get("abc")["status"] == "FAILED"


def test_drain_rejects_new_tasks(tmp_path):
    db, center = make_center(tmp_path)
    center.register("ok", lambda ctx, params: {})
    center.begin_drain()
    with pytest.raises(tc.TaskError):
        center.submit(spec("ok"))


def test_a07_append_log_publishes_sse_log_event(tmp_path):
    """ =A：append_log 随写随推 type=log 的 SSE 事件。"""
    db = make_db(tmp_path)

    class CaptureSSE:
        def __init__(self):
            self.events = []

        def publish(self, event):
            self.events.append(event)

    sse = CaptureSSE()
    center = TaskCenter(db, sse)
    center.append_log("task-x", "INFO", "hello log")
    log_events = [e for e in sse.events if e.get("type") == "log"]
    assert len(log_events) == 1
    assert log_events[0]["task_id"] == "task-x"
    assert log_events[0]["level"] == "INFO"
    assert log_events[0]["message"] == "hello log"
    assert db.scalar("SELECT COUNT(*) FROM task_log WHERE task_id='task-x'") == 1


def test_a08_recover_splits_stopping_and_running(tmp_path):
    """STOPPING → KILLED（用户主动停止），RUNNING → FAILED。"""
    db, center = make_center(tmp_path)
    db.execute(
        """
        INSERT INTO task(id, task_type, executor, market, source, lock_key, params, handler, status, progress, created_at)
        VALUES('run1','demo','fake.demo',NULL,'manual','demo','{}','slow','RUNNING',10,'2026-01-01T00:00:00')
        """
    )
    db.execute(
        """
        INSERT INTO task(id, task_type, executor, market, source, lock_key, params, handler, status, progress, created_at)
        VALUES('stop1','demo','fake.demo',NULL,'manual','demo','{}','slow','STOPPING',50,'2026-01-01T00:00:00')
        """
    )
    assert center.recover() == 2
    assert center.get("run1")["status"] == "FAILED"
    assert center.get("stop1")["status"] == "KILLED"


def test_active_count_and_kill_all(tmp_path):
    db, center = make_center(tmp_path)

    def slow(ctx, params):
        for _ in range(200):
            ctx.checkpoint()
            time.sleep(0.01)
        return {}

    center.register("slow", slow)
    task_id = center.submit(spec("slow"))
    wait_for(center, task_id, {"RUNNING"})
    assert center.active_count() == 1

    assert center.kill_all() == 1
    wait_for(center, task_id, {"KILLED"}, timeout=5.0)
    assert center.active_count() == 0
