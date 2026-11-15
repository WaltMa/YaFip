from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request

from yafip import __version__
from yafip.api.deps import get_ctx
from yafip.api.responses import ApiError, ok, page

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/config")
def get_config(request: Request):
    ctx = get_ctx(request)
    return ok(ctx.config.all())


@router.put("/config")
def put_config(request: Request, payload: dict[str, Any] = Body(...)):
    ctx = get_ctx(request)
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    if not values:
        raise ApiError("values 不能为空", "1001", 400)
    updated = ctx.config.set_many(values)
    ctx.audit.write("config.update", ",".join(values.keys()), str(values))
    return ok(updated)


@router.get("/system/health")
def health(request: Request):
    ctx = get_ctx(request)
    graceful = getattr(request.app.state, "graceful", None)
    try:
        ctx.db.scalar("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return ok(
        {
            "version": __version__,
            "db_ok": db_ok,
            "db_bytes": ctx.db.file_size(),
            "stopping": bool(getattr(graceful, "stopping", False)),
            "sse_subscribers": ctx.sse.subscriber_count,
            "tasks": ctx.tasks.status_counts(),
            "active_tasks": ctx.tasks.active_count(),
            "whitelist": ctx.whitelist.config_view(),
        }
    )


@router.get("/system/logs")
def logs(request: Request, lines: int = 200):
    ctx = get_ctx(request)
    path = Path(ctx.root) / "logs" / "yafip.log"
    if not path.exists():
        return ok({"path": str(path), "items": []})
    tail = deque(maxlen=max(1, min(2000, lines)))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            tail.append(line.rstrip("\n"))
    return ok({"path": str(path), "items": list(tail)})


@router.post("/system/stop")
def stop_service(request: Request, force: bool = False):
    """平滑停止：停止接收新任务，等待运行中的任务结束后退出。"""
    ctx = get_ctx(request)
    graceful = request.app.state.graceful
    accepted = graceful.request(force=force)
    ctx.audit.write("system.stop", "server", f"force={force} accepted={accepted}")
    return ok(
        {
            "stopping": True,
            "accepted": accepted,
            "force": bool(force or graceful.force),
            "running_tasks": ctx.tasks.active_count(),
        }
    )


@router.post("/system/vacuum")
def vacuum(request: Request):
    ctx = get_ctx(request)
    before = ctx.db.file_size()
    ctx.db.vacuum()
    after = ctx.db.file_size()
    ctx.audit.write("system.vacuum", "database", f"{before} -> {after}")
    return ok({"before": before, "after": after})


@router.post("/system/cleanup")
def cleanup(request: Request):
    """按 task.retention_days 清理终态任务、已处置质量问题与旧白名单 scope。"""
    ctx = get_ctx(request)
    days = ctx.config.get_int("task.retention_days", 90)
    cutoff = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%S")
    rows = ctx.db.query(
        """
        SELECT id FROM task
        WHERE status IN ('SUCCESS','FAILED','KILLED') AND created_at < ?
        """,
        (cutoff,),
    )
    ids = [str(r["id"]) for r in rows]
    if ids:
        # 集合式删除：两条语句完成，避免 2N 条逐行 DELETE 的长事务持锁
        # 阻塞抓取/日志写入（量大时直到 busy_timeout 超时）
        with ctx.db.tx() as conn:
            conn.execute(
                """
                DELETE FROM task_log
                WHERE task_id IN (
                    SELECT id FROM task
                    WHERE status IN ('SUCCESS','FAILED','KILLED') AND created_at < ?
                )
                """,
                (cutoff,),
            )
            conn.execute(
                """
                DELETE FROM task
                WHERE status IN ('SUCCESS','FAILED','KILLED') AND created_at < ?
                """,
                (cutoff,),
            )

    quality_deleted = ctx.repo.cleanup_resolved_issues(cutoff)

    # staging 保留策略清理（水位覆盖 / retention_days 兜底）
    watermarks = {
        str(row["market"]): row["last_trade_date"]
        for row in ctx.db.query("SELECT market, last_trade_date FROM sync_state")
    }
    staging_deleted = ctx.staging.cleanup(
        ctx.config.get_int("staging.retention_days", 30), watermarks
    )

    ctx.audit.write(
        "system.cleanup",
        f"before={cutoff}",
        f"tasks={len(ids)} quality={quality_deleted} staging={staging_deleted}",
    )
    return ok(
        {
            "cutoff": cutoff,
            "deleted": len(ids),
            "deleted_tasks": len(ids),
            "deleted_quality": quality_deleted,
            "deleted_staging": staging_deleted,
        }
    )


@router.get("/system/audit")
def audit(request: Request, page_number: int = 1, page_size: int = 50):
    ctx = get_ctx(request)
    items, total = ctx.audit.list(page=page_number, page_size=page_size)
    return ok(page(items, total, page_number, page_size))
