"""平滑停止控制。

stop 语义
1. 任务中心进入 drain 状态，拒绝新提交，已排队/运行中的任务继续；
2. 等待所有 PENDING / RUNNING / STOPPING 任务结束；
3. 通知 Uvicorn 退出（随后走正常 lifespan 关闭流程）。

force 语义：先取消所有任务，最多再等 30 秒；超时直接结束进程（极端情况兜底）。
server.stop_wait_sec > 0 时，普通 stop 也按该上限等待，超时强制退出。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from yafip import runtime

logger = logging.getLogger("yafip")

FORCE_WAIT_SEC = 30
REPORT_INTERVAL_SEC = 30


class GracefulStop:
    def __init__(self, app):
        self._app = app
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.stopping = False
        self.force = False

    def request(self, force: bool = False) -> bool:
        """请求停止；返回是否由本次调用启动停止流程。"""
        with self._lock:
            if self._thread is not None:
                if force and not self.force:
                    self.force = True
                    ctx = getattr(self._app.state, "ctx", None)
                    if ctx is not None:
                        ctx.tasks.kill_all()
                return False
            self.stopping = True
            self.force = force
            self._thread = threading.Thread(target=self._drain, name="yafip-stop", daemon=True)
            self._thread.start()
            return True

    def _drain(self) -> None:
        ctx = getattr(self._app.state, "ctx", None)
        server = getattr(self._app.state, "server", None)
        hard_exit = False
        try:
            if ctx is None:
                return
            logger.info("收到停止请求：停止接收新任务，等待运行中的任务结束")
            ctx.tasks.begin_drain()

            if self.force:
                killed = ctx.tasks.kill_all()
                logger.info("强制停止：已取消 %s 个任务", killed)
                wait_sec = FORCE_WAIT_SEC
            else:
                wait_sec = max(0, ctx.config.get_int("server.stop_wait_sec", 0))

            config_deadline = (
                time.monotonic() + wait_sec if (wait_sec > 0 and not self.force) else None
            )
            force_deadline = time.monotonic() + FORCE_WAIT_SEC if self.force else None
            last_report = time.monotonic()
            while True:
                active = ctx.tasks.active_count()
                if active <= 0:
                    break
                now = time.monotonic()
                # 普通 stop 执行期间收到 force 请求时，补上强制等待上限
                if self.force and force_deadline is None:
                    force_deadline = now + FORCE_WAIT_SEC
                if now - last_report >= REPORT_INTERVAL_SEC:
                    logger.info("等待任务结束：仍有 %s 个任务运行中", active)
                    last_report = now
                if force_deadline is not None and now >= force_deadline:
                    logger.warning("强制停止等待超时，仍有 %s 个任务未完成，强制退出", active)
                    hard_exit = True
                    break
                if config_deadline is not None and now >= config_deadline:
                    logger.warning("停止等待超时（%s 秒），仍有 %s 个任务未完成，强制退出", wait_sec, active)
                    hard_exit = True
                    break
                time.sleep(1.0)
            if not hard_exit:
                logger.info("任务已全部结束，服务即将退出")
        except Exception:  # noqa: BLE001 - 停止流程异常也必须退出
            logger.exception("停止流程异常，直接退出")
            hard_exit = True
        finally:
            if hard_exit:
                root = getattr(ctx, "root", None) if ctx is not None else None
                if root is not None:
                    runtime.clear_run_info(root, os.getpid())
                os._exit(1)
            if server is not None:
                server.should_exit = True
