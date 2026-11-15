from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from yafip.api.deps import get_ctx
from yafip.api.responses import ApiError, ok, page

router = APIRouter(prefix="/api/tasks", tags=["task"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _decode(record: dict) -> dict:
    item = dict(record)
    for field in ("params", "result"):
        value = item.get(field)
        if isinstance(value, str) and value:
            try:
                item[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return item


# 注意：/stream 必须声明在 /{task_id} 之前
@router.get("/stream")
async def stream_all(request: Request):
    ctx = get_ctx(request)
    # 兜底下限：历史库若已写入 0（旧版本无校验），heartbeat=0 会让 wait_for 立即超时、无限空转
    heartbeat = max(1, ctx.config.get_int("sse.heartbeat_sec", 15))

    async def generator():
        async for event in ctx.sse.stream(None, heartbeat):
            if event.get("type") == "heartbeat":
                yield ": ping\n\n"
            else:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    ctx = get_ctx(request)
    heartbeat = max(1, ctx.config.get_int("sse.heartbeat_sec", 15))

    async def generator():
        async for event in ctx.sse.stream(task_id, heartbeat):
            if event.get("type") == "heartbeat":
                yield ": ping\n\n"
            else:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("")
def list_tasks(
    request: Request,
    status: str | None = None,
    source: str | None = None,
    task_type: str | None = None,
    executor: str | None = None,
    market: str | None = None,
    keyword: str | None = None,
    start: str | None = None,
    end: str | None = None,
    page_number: int = 1,
    page_size: int = 20,
):
    ctx = get_ctx(request)
    items, total = ctx.tasks.list(
        status=status,
        source=source,
        task_type=task_type,
        executor=executor,
        market=market,
        keyword=keyword,
        start=start,
        end=end,
        page=page_number,
        page_size=page_size,
    )
    return ok(page([_decode(i) for i in items], total, page_number, page_size))


@router.get("/{task_id}")
def get_task(task_id: str, request: Request):
    ctx = get_ctx(request)
    return ok(_decode(ctx.tasks.get(task_id)))


@router.get("/{task_id}/logs")
def get_logs(task_id: str, request: Request, since_id: int = 0, limit: int = 500):
    ctx = get_ctx(request)
    ctx.tasks.get(task_id)
    return ok({"items": ctx.tasks.logs(task_id, since_id=since_id, limit=limit)})


@router.post("/{task_id}/kill")
def kill_task(task_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.tasks.kill(task_id)
    ctx.audit.write("task.kill", task_id)
    return ok({"task_id": task_id, "status": "STOPPING"})


@router.post("/{task_id}/retry", status_code=202)
def retry_task(task_id: str, request: Request):
    ctx = get_ctx(request)
    new_id = ctx.tasks.retry(task_id)
    ctx.audit.write("task.retry", task_id, f"new={new_id}")
    return ok({"task_id": new_id, "retry_of": task_id})


@router.delete("/{task_id}")
def delete_task(task_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.tasks.delete(task_id)
    ctx.audit.write("task.delete", task_id)
    return ok({"deleted": task_id})
