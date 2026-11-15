from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from yafip.api.deps import get_ctx
from yafip.api.responses import ApiError, ok, page
from yafip.data.market import MARKET_CN_A

router = APIRouter(prefix="/api/data", tags=["dataquery"])


@router.get("/instruments")
def instruments(
    request: Request,
    market: str | None = None,
    keyword: str | None = None,
    asset_type: str | None = None,
    page_number: int = 1,
    page_size: int = 50,
):
    ctx = get_ctx(request)
    items, total = ctx.repo.instruments(
        market=market,
        keyword=keyword,
        asset_type=asset_type,
        page=page_number,
        page_size=page_size,
    )
    return ok(page(items, total, page_number, page_size))


@router.get("/daily")
def daily(
    request: Request,
    market: str = MARKET_CN_A,
    symbol: str | None = None,
    ts_code: str | None = None,  # 兼容旧前端：无 symbol 时按 ts_code 兜底（CN_A 同值）
    start: str | None = None,
    end: str | None = None,
    price_type: str = "raw",
    limit: int = 5000,
):
    ctx = get_ctx(request)
    symbol = symbol or ts_code
    if not symbol:
        raise ApiError("缺少 symbol 参数", "4001", 400)
    try:
        end = end or date.today().isoformat()
        start = start or (date.fromisoformat(end) - timedelta(days=365)).isoformat()
    except ValueError:
        raise ApiError("日期格式必须为 YYYY-MM-DD", "4001", 400) from None
    if price_type not in {"raw", "qfq", "hfq"}:
        price_type = "raw"
    items = ctx.repo.daily_query(market, symbol, start, end, price_type=price_type, limit=limit)
    return ok({"market": market, "symbol": symbol, "price_type": price_type, "start": start, "end": end, "items": items})


@router.get("/coverage")
def coverage(request: Request, market: str = MARKET_CN_A, granularity: str = "year"):
    ctx = get_ctx(request)
    return ok(ctx.repo.coverage(market, granularity if granularity in {"year", "month"} else "year"))


class ResolveBody(BaseModel):
    ids: list[int] = Field(default_factory=list)


@router.get("/quality-issues")
def quality_issues(
    request: Request,
    check_type: str | None = None,
    market: str | None = None,
    resolved: int | None = None,
    page_number: int = 1,
    page_size: int = 50,
):
    ctx = get_ctx(request)
    items, total = ctx.repo.issues(
        check_type=check_type,
        market=market,
        resolved=resolved,
        page=page_number,
        page_size=page_size,
    )
    return ok(page(items, total, page_number, page_size))


@router.post("/quality-issues/resolve")
def resolve_issues(body: ResolveBody, request: Request):
    ctx = get_ctx(request)
    count = ctx.repo.resolve_issues(body.ids)
    if body.ids:
        ctx.audit.write("quality.resolve", ",".join(str(i) for i in body.ids[:50]), f"count={count}")
    return ok({"resolved": count})


@router.get("/progress")
def progress(request: Request, market: str = MARKET_CN_A):
    """水位视图（替代 fetch_progress 断点视图）。"""
    ctx = get_ctx(request)
    watermark = ctx.repo.sync_state_watermark(market)
    latest = ctx.repo.latest_bar_date(market)
    next_open = None
    if watermark:
        tomorrow = (date.fromisoformat(watermark) + timedelta(days=1)).isoformat()
        next_open = ctx.repo.latest_open_date(market, date.today().isoformat())
        if next_open and next_open <= watermark:
            next_open = None
    return ok(
        {
            "market": market,
            "watermark": watermark,
            "next_trade_date": next_open,
            "latest_bar_date": latest,
            "whitelist_size": len(ctx.whitelist.state(market)),
        }
    )
