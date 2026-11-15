from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from yafip.api.deps import get_ctx
from yafip.api.responses import ok
from yafip.data.executors import TASK_TYPE_LABELS
from yafip.data.market import MARKET_CN_A

router = APIRouter(prefix="/api", tags=["executors"])


def _serialize(spec) -> dict:
    return {
        "name": spec.name,
        "task_type": spec.task_type,
        "task_type_label": TASK_TYPE_LABELS.get(spec.task_type, spec.task_type),
        "source": spec.source,
        "markets": list(spec.markets),
        "title": spec.title,
        "params_schema": spec.params_schema,
        "implemented": spec.implemented,
    }


@router.get("/executors")
def list_executors(
    request: Request,
    market: str | None = None,
    task_type: str | None = None,
):
    """注册表视图： 动态表单据此渲染「市场 → 任务类型 → 执行器 → 参数」。"""
    ctx = get_ctx(request)
    specs = ctx.executors.list(market=market, task_type=task_type)
    return ok({"items": [_serialize(s) for s in specs]})


class TaskSubmitBody(BaseModel):
    task_type: str
    executor: str
    market: str = MARKET_CN_A
    params: dict[str, Any] = {}


@router.post("/tasks", status_code=202)
def create_task(body: TaskSubmitBody, request: Request):
    """统一任务提交入口：注册中心校验（不符 400），成功后 202 + task_id。"""
    ctx = get_ctx(request)
    task_id = ctx.data.submit(body.task_type, body.executor, body.market, body.params or {})
    ctx.audit.write("executor.submit", body.executor, f"{body.task_type} {body.market}")
    return ok({"task_id": task_id})
