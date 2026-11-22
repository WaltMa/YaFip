"""通用异步任务底座。

与业务完全解耦：只认识 TaskSpec / handler 名，不知道"抓取"是什么。
触发方只能是上层业务（DataService / 未来策略系统）。

CPU 与并发限流完整实现——
- 全局并发上限（默认 10），超出保持 PENDING 排队，不占用工作线程；
- 单任务占空比节流：每工作单元后按 process_time() 计量 CPU，sleep = cpu × (1/budget − 1)，
  IO 等待不计入；验证口径 60s 采样 CPU 时间/墙钟 ≤ budget 核；
- 全局预算分摊：per_task_budget = min(cpu_ratio, cpu_budget_ratio / active_tasks)，
  防止 1 核机器被并发任务打满；
- DB 提交单写队列（见 data/staging.py 的提交锁）。
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from yafip.core.cancel_token import CancelToken, TaskCancelled
from yafip.core.db import Database, now_iso
from yafip.core.sse import SSEBroker

TERMINAL = {"SUCCESS", "FAILED", "KILLED"}
ACTIVE = {"PENDING", "RUNNING", "STOPPING"}

# 默认限流参数（config.py 提供同名可配键，未传 config 时用此默认值）
MAX_CONCURRENT_TASKS = 10
TASK_CPU_RATIO = 0.10
GLOBAL_CPU_BUDGET_RATIO = 0.50
# 节流睡眠上限（秒）：避免极端 CPU 密集单元造成任务"假死"；占空比保持近似
THROTTLE_SLEEP_CAP = 10.0


class TaskError(Exception):
    code = "2000"
    http_status = 400


class TaskNotFound(TaskError):
    code = "2001"
    http_status = 404


class TaskConflict(TaskError):
    code = "2002"
    http_status = 409


class TaskNotKillable(TaskError):
    code = "2003"
    http_status = 409


class TaskNotRetryable(TaskError):
    code = "2004"
    http_status = 409


@dataclass
class TaskSpec:
    task_type: str                      # meta.sync_all / bars.incremental / ...
    executor: str                       # tushare.bars.incremental 等
    market: str | None                  # CN_A / HK / US / None（跨市场）
    source: str
    params: dict[str, Any]
    handler: str
    lock_key: str                       # (task_type, market) 派生

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskContext:
    center: "TaskCenter"
    task_id: str
    token: CancelToken
    _logged: int = field(default=0, repr=False)
    _cpu_last: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self._cpu_last = time.process_time()

    # -- 取消 ---------------------------------------------------------------
    @property
    def is_cancelled(self) -> bool:
        return self.token.cancelled

    def checkpoint(self) -> None:
        self.token.checkpoint()
        self._throttle()

    def _throttle(self) -> None:
        """占空比节流：按本工作单元 CPU 时间让出 CPU 份额（IO 等待不计入）。"""
        now = time.process_time()
        elapsed_cpu = now - self._cpu_last
        self._cpu_last = now
        budget = self.center.current_budget()
        if budget <= 0 or elapsed_cpu <= 0:
            return
        sleep = elapsed_cpu * (1.0 / budget - 1.0)
        if sleep > 0:
            time.sleep(min(sleep, THROTTLE_SLEEP_CAP))

    # -- 进度 / 日志 --------------------------------------------------------
    def progress(self, percent: float, stage: str | None = None, message: str | None = None) -> None:
        self.center.update_progress(self.task_id, percent, stage, message)

    def log(self, level: str, message: str) -> None:
        self.center.append_log(self.task_id, level, message)


class TaskCenter:
    def __init__(
        self,
        db: Database,
        sse: SSEBroker,
        max_workers: int = 2,  # 弃用：并发由 task.max_concurrency（信号量）控制，本参数仅兼容旧调用
        config: Any = None,
    ):
        self.db = db
        self.sse = sse
        self.config = config
        self.max_concurrency = (
            config.get_int("task.max_concurrency", MAX_CONCURRENT_TASKS)
            if config is not None
            else MAX_CONCURRENT_TASKS
        )
        self.cpu_ratio = (
            config.get_float("task.cpu_ratio", TASK_CPU_RATIO)
            if config is not None
            else TASK_CPU_RATIO
        )
        self.cpu_budget_ratio = (
            config.get_float("task.cpu_budget_ratio", GLOBAL_CPU_BUDGET_RATIO)
            if config is not None
            else GLOBAL_CPU_BUDGET_RATIO
        )
        # 线程池至少能容纳并发上限，信号量负责真正的并发闸门；
        # 不再叠加 max_workers（原配置已下线，实为死配置）
        pool_workers = max(2, self.max_concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=pool_workers, thread_name_prefix="yafip-task"
        )
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._waiting: list[str] = []  # 排队任务（PENDING，未占用工作线程）
        self._active = 0               # 运行中任务数（预算分摊用）
        self._handlers: dict[str, Callable[[TaskContext, dict], Any]] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._lock = threading.RLock()
        self._accepting = True

    # -- 预算分摊 -----------------------------------------------------------
    def current_budget(self) -> float:
        """per_task_budget = min(cpu_ratio, cpu_budget_ratio / active_tasks)。"""
        with self._lock:
            active = max(1, self._active)
        return min(self.cpu_ratio, self.cpu_budget_ratio / active)

    # -- 注册 ---------------------------------------------------------------
    def register(self, name: str, handler: Callable[[TaskContext, dict], Any]) -> None:
        self._handlers[name] = handler

    # -- 提交 ---------------------------------------------------------------
    def submit(self, spec: TaskSpec, retry_of: str | None = None) -> str:
        with self._lock:
            if not self._accepting:
                raise TaskError("服务正在停止，暂不接受新任务")
        if spec.handler not in self._handlers:
            raise TaskError(f"未注册的 handler：{spec.handler}")
        if not spec.lock_key:
            raise TaskError("缺少 lock_key（应为 task_type:market 派生）")

        task_id = uuid.uuid4().hex
        now = now_iso()
        params_json = json.dumps(spec.params, ensure_ascii=False, default=str)

        with self.db.tx() as conn:
            conflict = conn.execute(
                """
                SELECT id, status FROM task
                WHERE lock_key=? AND status IN ('PENDING','RUNNING','STOPPING')
                LIMIT 1
                """,
                (spec.lock_key,),
            ).fetchone()
            if conflict is not None:
                raise TaskConflict(
                    f"已有同类任务在运行（{conflict['id'][:8]}，{conflict['status']}）"
                )
            conn.execute(
                """
                INSERT INTO task(
                    id, task_type, executor, market, source, lock_key, params, handler,
                    status, progress, created_at, retry_of
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    spec.task_type,
                    spec.executor,
                    spec.market,
                    spec.source,
                    spec.lock_key,
                    params_json,
                    spec.handler,
                    "PENDING",
                    0.0,
                    now,
                    retry_of,
                ),
            )

        with self._lock:
            # 与 begin_drain 竞态兜底：插入后再次确认服务未进入停止流程
            if not self._accepting:
                self.db.execute(
                    """
                    UPDATE task SET status='KILLED', finished_at=?, message=?
                    WHERE id=? AND status='PENDING'
                    """,
                    (now_iso(), "服务正在停止，任务已取消", task_id),
                )
                raise TaskError("服务正在停止，任务已取消")
            self._tokens[task_id] = CancelToken()

        try:
            self._publish(task_id)
            self.append_log(task_id, "INFO", f"任务已提交：{spec.task_type}/{spec.executor}")
            if not self._try_start(task_id, spec.handler, spec.params):
                self.append_log(task_id, "INFO", "任务排队中（并发已达上限）")
                with self._lock:
                    self._waiting.append(task_id)
        except Exception as exc:  # noqa: BLE001 - 编排失败不留下幽灵任务
            # PENDING 已落库但 publish/log/start 异常：清 token 并把任务落终态，
            # 否则该 lock_key 被永久占用，后续同类型任务全部冲突
            self._cleanup(task_id)
            try:
                self.db.execute(
                    """
                    UPDATE task SET status='FAILED', error=?, finished_at=?, stage='中断'
                    WHERE id=? AND status='PENDING'
                    """,
                    (f"任务提交后编排失败：{exc}", now_iso(), task_id),
                )
            except Exception:  # noqa: BLE001 - 兜底标记失败
                pass
            raise TaskError(f"任务提交失败：{exc}") from exc
        return task_id

    # -- 并发闸门 -----------------------------------------------------------
    def _try_start(self, task_id: str, handler: str, params: dict) -> bool:
        """获取并发许可后启动；失败返回 False（任务进入排队）。"""
        if not self._semaphore.acquire(blocking=False):
            return False
        self._executor.submit(self._run, task_id, handler, params)
        return True

    def _pump_waiting(self) -> None:
        """运行任务结束释放许可后，依次启动排队任务。"""
        while True:
            with self._lock:
                if not self._waiting or not self._semaphore.acquire(blocking=False):
                    return
                task_id = self._waiting.pop(0)
            try:
                record = self.get(task_id)
                if record["status"] != "PENDING":
                    # 排队期间被杀/删除：释放许可继续
                    self._semaphore.release()
                    continue
                self._executor.submit(
                    self._run, task_id, record["handler"],
                    json.loads(record["params"] or "{}"),
                )
            except Exception:  # noqa: BLE001 - 排队启动失败不阻断
                self._semaphore.release()

    # -- 执行（工作线程） ---------------------------------------------------
    def _run(self, task_id: str, handler_name: str, params: dict) -> None:
        with self._lock:
            self._active += 1
            token = self._tokens.setdefault(task_id, CancelToken())
        try:
            self._run_inner(task_id, handler_name, params, token)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
            self._semaphore.release()
            self._pump_waiting()

    def _run_inner(self, task_id: str, handler_name: str, params: dict, token: CancelToken) -> None:
        record = self.get(task_id)
        if record["status"] in TERMINAL:
            # 提交后在 PENDING 阶段被杀的任务：这里直接退出，但要清掉 _tokens，
            # 否则 CancelToken 永久残留（kill 只在 PENDING 时 _finish 不触发 _cleanup）
            self._cleanup(task_id)
            return
        if token.cancelled:
            self._finish(task_id, "KILLED", message="任务在启动前被取消")
            return

        handler = self._handlers.get(handler_name)
        if handler is None:
            self._finish(task_id, "FAILED", error=f"handler 未注册：{handler_name}")
            return

        self.db.execute(
            "UPDATE task SET status='RUNNING', started_at=?, stage='执行中' WHERE id=? AND status='PENDING'",
            (now_iso(), task_id),
        )
        # 若在 PENDING->RUNNING 之间被杀，直接退出
        if self.get(task_id)["status"] == "KILLED":
            self._cleanup(task_id)
            return
        self._publish(task_id)

        ctx = TaskContext(self, task_id, token)
        try:
            result = handler(ctx, params)
            self._finish(task_id, "SUCCESS", result=result or {})
        except TaskCancelled:
            self._finish(task_id, "KILLED", message="任务已被取消")
        except Exception as exc:  # noqa: BLE001 - 任务边界必须兜底
            self._finish(
                task_id,
                "FAILED",
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )
        finally:
            self._cleanup(task_id)

    def _cleanup(self, task_id: str) -> None:
        with self._lock:
            self._tokens.pop(task_id, None)

    def _finish(
        self,
        task_id: str,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
        message: str | None = None,
    ) -> None:
        progress = 100.0 if status == "SUCCESS" else None
        sets = ["status=?", "finished_at=?"]
        values: list[Any] = [status, now_iso()]
        if progress is not None:
            sets.append("progress=?")
            values.append(progress)
        if result is not None:
            sets.append("result=?")
            values.append(json.dumps(result, ensure_ascii=False, default=str)[:200000])
        if error:
            sets.append("error=?")
            values.append(error[:20000])
        if message:
            sets.append("message=?")
            values.append(message)
        if status == "SUCCESS":
            sets.append("stage='完成'")
        values.append(task_id)
        # 状态守卫：只允许从非终态转移，防止 shutdown/kill 落终态后，
        # 运行线程的迟到 SUCCESS/FAILED 覆盖（互斥失效）
        self.db.execute(
            f"UPDATE task SET {', '.join(sets)} WHERE id=? AND status IN ('PENDING','RUNNING','STOPPING')",
            values,
        )
        if status == "SUCCESS" and result:
            self.append_log(task_id, "INFO", f"任务完成：{json.dumps(result, ensure_ascii=False, default=str)[:1000]}")
        elif status == "FAILED":
            self.append_log(task_id, "ERROR", (error or "任务失败")[:1000])
        self._publish(task_id)

    # -- 进度 / 日志 --------------------------------------------------------
    def update_progress(
        self,
        task_id: str,
        percent: float,
        stage: str | None = None,
        message: str | None = None,
    ) -> None:
        percent = max(0.0, min(100.0, float(percent)))
        sets = ["progress=?"]
        values: list[Any] = [percent]
        if stage is not None:
            sets.append("stage=?")
            values.append(stage)
        if message is not None:
            sets.append("message=?")
            values.append(message)
        values.append(task_id)
        self.db.execute(f"UPDATE task SET {', '.join(sets)} WHERE id=?", values)
        self._publish(task_id)

    def append_log(self, task_id: str, level: str, message: str) -> None:
        ts = now_iso()
        lvl = level.upper()
        text = str(message)[:4000]
        self.db.execute(
            "INSERT INTO task_log(task_id, ts, level, message) VALUES(?,?,?,?)",
            (task_id, ts, lvl, text),
        )
        #  =A：日志随写随推 SSE（sse.publish 线程安全，内部吞异常）
        self.sse.publish({"type": "log", "task_id": task_id, "ts": ts, "level": lvl, "message": text})

    # -- 查询 ---------------------------------------------------------------
    def get(self, task_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
        if row is None:
            raise TaskNotFound(f"任务不存在：{task_id}")
        return dict(row)

    def list(
        self,
        *,
        status: str | None = None,
        source: str | None = None,
        task_type: str | None = None,
        executor: str | None = None,
        market: str | None = None,
        keyword: str | None = None,
        start: str | None = None,
        end: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            statuses = [s for s in status.split(",") if s]
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if source:
            where.append("source=?")
            params.append(source)
        if task_type:
            where.append("task_type=?")
            params.append(task_type)
        if executor:
            where.append("executor=?")
            params.append(executor)
        if market:
            where.append("market=?")
            params.append(market)
        if keyword:
            where.append("(id LIKE ? OR stage LIKE ? OR message LIKE ? OR task_type LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        if start:
            where.append("created_at >= ?")
            params.append(start)
        if end:
            where.append("created_at <= ?")
            params.append(end)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM task {clause}", params, default=0))
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        rows = self.db.query(
            f"SELECT * FROM task {clause} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )
        return [dict(r) for r in rows], total

    def logs(self, task_id: str, since_id: int = 0, limit: int = 500) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM task_log WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (task_id, since_id, max(1, min(2000, limit))),
        )
        return [dict(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM task GROUP BY status")
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        for key in ("PENDING", "RUNNING", "STOPPING", "SUCCESS", "FAILED", "KILLED"):
            counts.setdefault(key, 0)
        return counts

    # -- 控制 ---------------------------------------------------------------
    def kill(self, task_id: str) -> bool:
        record = self.get(task_id)
        status = record["status"]
        if status in TERMINAL:
            raise TaskNotKillable(f"任务已结束（{status}），无法杀死")

        with self._lock:
            token = self._tokens.setdefault(task_id, CancelToken())
            token.cancel()

        if status == "STOPPING":
            # 幂等：已在停止中（首次 kill 已 cancel token），重复 kill 直接成功；
            # 否则 _mark_stopping 的 RUNNING 守卫会 0 行并把 STOPPING 误当终态抛 409。
            return True
        if status == "PENDING":
            # 守卫更新：读取状态到写入之间任务可能已进入 RUNNING/终态，
            # 无谓词 UPDATE 会把已 SUCCESS 的任务改回 KILLED（互斥失效）
            updated = self.db.execute(
                """
                UPDATE task SET status='KILLED', finished_at=?, message=?, stage='中断'
                WHERE id=? AND status='PENDING'
                """,
                (now_iso(), "任务在排队阶段被取消", task_id),
            )
            if not updated:
                current = self.get(task_id)
                if current["status"] in TERMINAL:
                    raise TaskNotKillable(f"任务已结束（{current['status']}），无法杀死")
                return self._mark_stopping(task_id)
            self.append_log(task_id, "WARN", "排队任务已取消")
            self._publish(task_id)
            return True
        return self._mark_stopping(task_id)

    def _mark_stopping(self, task_id: str) -> bool:
        """RUNNING → STOPPING（守卫更新；0 行说明任务已在读取后结束）。"""
        updated = self.db.execute(
            """
            UPDATE task SET status='STOPPING', message='正在停止：等待当前交易单元结束'
            WHERE id=? AND status='RUNNING'
            """,
            (task_id,),
        )
        if not updated:
            current = self.get(task_id)
            if current["status"] in TERMINAL:
                raise TaskNotKillable(f"任务已结束（{current['status']}），无法杀死")
            if current["status"] == "STOPPING":
                # 竞态：读取状态后已被其他 kill 置 STOPPING，幂等成功
                return True
            raise TaskNotKillable(f"任务状态异常：{current['status']}")  # 理论不可达，兜底
        self.append_log(task_id, "WARN", "收到取消请求，将在当前交易单元结束后退出")
        self._publish(task_id)
        return True

    def retry(self, task_id: str) -> str:
        record = self.get(task_id)
        if record["status"] not in TERMINAL:
            raise TaskNotRetryable(f"任务尚未结束（{record['status']}），不能重跑")
        params = json.loads(record["params"] or "{}")
        spec = TaskSpec(
            task_type=record["task_type"],
            executor=record["executor"],
            market=record.get("market"),
            source="manual",
            params=params,
            handler=record["handler"],
            lock_key=record["lock_key"],
        )
        return self.submit(spec, retry_of=task_id)

    def delete(self, task_id: str) -> None:
        record = self.get(task_id)
        if record["status"] not in TERMINAL:
            raise TaskNotRetryable(f"任务尚未结束（{record['status']}），不能删除")
        with self.db.tx() as conn:
            conn.execute("DELETE FROM task_log WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM task WHERE id=?", (task_id,))

    # -- 启动恢复 -----------------------------------------------------------
    def recover(self) -> int:
        """拆分语义：STOPPING 是用户主动停止 → KILLED；RUNNING 是被迫中断 → FAILED；
        PENDING 是排队未启动（Ctrl+C 等退出路径遗留）→ FAILED，释放 lock_key。"""
        rows = self.db.query(
            "SELECT id, status FROM task WHERE status IN ('PENDING','RUNNING','STOPPING')"
        )
        for row in rows:
            if row["status"] == "STOPPING":
                self.db.execute(
                    """
                    UPDATE task SET status='KILLED', error=?, finished_at=?, stage='中断'
                    WHERE id=? AND status='STOPPING'
                    """,
                    ("服务重启，停止中的任务被标记为已杀死", now_iso(), row["id"]),
                )
                self.append_log(row["id"], "WARN", "服务重启，停止中的任务被标记为已杀死")
            elif row["status"] == "PENDING":
                self.db.execute(
                    """
                    UPDATE task SET status='FAILED', error=?, finished_at=?, stage='中断'
                    WHERE id=? AND status='PENDING'
                    """,
                    ("服务重启，排队中的任务被取消（未开始执行）", now_iso(), row["id"]),
                )
                self.append_log(row["id"], "ERROR", "服务重启，排队中的任务被取消")
            else:
                self.db.execute(
                    """
                    UPDATE task SET status='FAILED', error=?, finished_at=?, stage='中断'
                    WHERE id=? AND status='RUNNING'
                    """,
                    ("服务重启导致中断，可重跑（断点会自动续传）", now_iso(), row["id"]),
                )
                self.append_log(row["id"], "ERROR", "服务重启，任务被标记为失败")
        return len(rows)

    # -- 启停 ---------------------------------------------------------------
    def begin_drain(self) -> None:
        """停止接收新任务；已排队/运行中的任务继续执行。"""
        with self._lock:
            self._accepting = False

    def active_count(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM task WHERE status IN ('PENDING','RUNNING','STOPPING')",
                default=0,
            )
            or 0
        )

    def kill_all(self) -> int:
        rows = self.db.query("SELECT id FROM task WHERE status IN ('PENDING','RUNNING','STOPPING')")
        killed = 0
        for row in rows:
            try:
                self.kill(str(row["id"]))
                killed += 1
            except TaskError:
                continue
        return killed

    def shutdown(self, wait_timeout: float = 0.0) -> None:
        """关闭线程池；把残留 PENDING/RUNNING 统一落终态（FAILED）。

        - 先取消未启动的 future；wait_timeout>0 时等运行中任务结束（有界）；
        - 超时/直接调用时把残留 PENDING/RUNNING 标记 FAILED，避免 Ctrl+C 后
          排队任务永远 PENDING 占着 lock_key。
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
        if wait_timeout and wait_timeout > 0:
            deadline = time.monotonic() + wait_timeout
            while time.monotonic() < deadline:
                if self.active_count() == 0:
                    break
                time.sleep(0.2)
        self.db.execute(
            """
            UPDATE task SET status='FAILED', error=?, finished_at=?, stage='中断'
            WHERE status IN ('PENDING','RUNNING')
            """,
            ("服务关闭导致中断，可重跑（断点会自动续传）", now_iso()),
        )

    # -- SSE ----------------------------------------------------------------
    def _publish(self, task_id: str) -> None:
        try:
            record = self.get(task_id)
        except TaskNotFound:
            return
        self.sse.publish(
            {
                "type": "task",
                "task_id": task_id,
                "status": record["status"],
                "progress": record["progress"],
                "stage": record["stage"],
                "message": record["message"],
                "task_type": record["task_type"],
                "executor": record["executor"],
                "market": record["market"],
                "source": record["source"],
                "finished_at": record["finished_at"],
            }
        )
