from __future__ import annotations

from fastapi import APIRouter, Request

from yafip.api.deps import get_ctx
from yafip.api.responses import ok
from yafip.data.market import MARKET_CN_A

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
def overview(request: Request):
    ctx = get_ctx(request)
    stats = ctx.repo.overview_stats()
    recent, _ = ctx.tasks.list(page=1, page_size=8)
    datasource = ctx.repo.get_datasource("tushare") or {}
    watermark = ctx.repo.sync_state_watermark(MARKET_CN_A)
    return ok(
        {
            **stats,
            "tasks": ctx.tasks.status_counts(),
            "recent_tasks": recent,
            "datasource": {
                "name": "tushare",
                "has_token": bool(datasource.get("token")),
                "enabled": bool(datasource.get("enabled", 0)),
                "last_test_at": datasource.get("last_test_at"),
                "last_test_ok": datasource.get("last_test_ok"),
                "last_test_msg": datasource.get("last_test_msg"),
            },
            "watermark": watermark,
        }
    )
