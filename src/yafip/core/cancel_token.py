"""协作式取消令牌。

Python 线程无法强制终止，任务体必须在单元边界调用 checkpoint()。
"""
from __future__ import annotations

import threading


class TaskCancelled(Exception):
    """任务在检查点发现取消请求。"""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def checkpoint(self) -> None:
        if self._event.is_set():
            raise TaskCancelled("任务已被取消")
