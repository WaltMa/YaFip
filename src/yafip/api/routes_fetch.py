from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from yafip.api.deps import get_ctx
from yafip.api.responses import ok
from yafip.data.market import MARKET_CN_A

router = APIRouter(prefix="/api/data", tags=["fetch"])


class QualityBody(BaseModel):
    market: str = MARKET_CN_A
    start: str | None = None
    end: str | None = None
    symbols: list[str] = Field(default_factory=list)


@router.post("/quality-scan", status_code=202)
def quality_scan(body: QualityBody, request: Request):
    """质量扫描（辅助任务，不入 5 类主任务注册表）。"""
    ctx = get_ctx(request)
    task_id = ctx.data.create_quality_scan(body.model_dump())
    return ok({"task_id": task_id})
