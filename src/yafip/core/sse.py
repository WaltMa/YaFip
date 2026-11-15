"""SSE 广播：任务线程 -> asyncio 事件循环的线程安全投递。"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncIterator


class SSEBroker:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- 发布（任意线程） ---------------------------------------------------
    def publish(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            queues = list(self._subs)
        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                pass

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # 客户端太慢：丢最旧一条，保留最新进度
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # -- 订阅（事件循环内） -------------------------------------------------
    async def stream(
        self,
        task_id: str | None = None,
        heartbeat_sec: int = 15,
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(queue)
        try:
            yield {"type": "connected", "task_id": task_id}
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_sec)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                if task_id is None or event.get("task_id") == task_id:
                    yield event
        finally:
            with self._lock:
                self._subs.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
