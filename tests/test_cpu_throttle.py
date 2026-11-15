from __future__ import annotations

import time

from conftest import make_db

from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskContext, TaskCenter


class FakeCenter:
    def __init__(self, budget: float):
        self._budget = budget

    def current_budget(self) -> float:
        return self._budget


def test_current_budget_formula(tmp_path):
    """per_task_budget = min(cpu_ratio, cpu_budget_ratio / active_tasks)。"""
    db = make_db(tmp_path)
    config = None
    center = TaskCenter(db, SSEBroker(), config=config)
    try:
        # 默认：ratio 0.10, budget 0.50
        with center._lock:
            center._active = 1
        assert abs(center.current_budget() - 0.10) < 1e-9  # min(0.10, 0.50)
        with center._lock:
            center._active = 10
        assert abs(center.current_budget() - 0.05) < 1e-9  # min(0.10, 0.50/10)
        with center._lock:
            center._active = 0
        assert abs(center.current_budget() - 0.10) < 1e-9  # active=0 → max(1,0)=1
    finally:
        db.close_all()


class FakeToken:
    def checkpoint(self) -> None:
        return None


def test_throttle_sleep_duty_cycle(monkeypatch):
    """sleep = cpu × (1/budget − 1)；IO 等待不计入（process_time 不随 sleep 前进）。"""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    # 必须先 patch：TaskContext.__post_init__ 也消费一次 process_time 作为起点
    cpu_values = iter([0.02, 0.04, 0.09, 0.14])  # 起点0.02；单元1 cpu=0.02；单元2 cpu=0.05
    monkeypatch.setattr(time, "process_time", lambda: next(cpu_values))

    center = FakeCenter(budget=0.10)
    ctx = TaskContext(center, "t1", FakeToken())  # type: ignore[arg-type]

    ctx.checkpoint()  # cpu=0.02 → sleep = 0.02*(1/0.1-1)=0.18
    assert abs(sleeps[0] - 0.18) < 1e-9
    ctx.checkpoint()  # cpu=0.05 → sleep = 0.05*9=0.45
    assert abs(sleeps[1] - 0.45) < 1e-9

    # 低预算分摊：budget 0.05 → 放大 19 倍
    sleeps2: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps2.append(seconds))
    cpu2 = iter([0.01, 0.02])  # 起点0.01；单元1 cpu=0.01
    monkeypatch.setattr(time, "process_time", lambda: next(cpu2))
    center2 = FakeCenter(budget=0.05)
    ctx2 = TaskContext(center2, "t2", FakeToken())  # type: ignore[arg-type]
    ctx2.checkpoint()
    assert abs(sleeps2[0] - 0.19) < 1e-9
