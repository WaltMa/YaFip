from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from yafip.api.deps import get_ctx
from yafip.api.responses import ApiError, ok, page
from yafip.data.market import MARKET_CN_A

router = APIRouter(prefix="/api/whitelist", tags=["whitelist"])


class WhitelistConfigBody(BaseModel):
    metric: str | None = None
    threshold_yi: float | None = None
    threshold_wan: float | None = None


class MemberBody(BaseModel):
    market: str | None = None
    symbol: str | None = None
    reason: str | None = None


@router.get("/config")
def get_config(request: Request, market: str | None = None):
    ctx = get_ctx(request)
    return ok(ctx.whitelist.config_view(market))


@router.put("/config")
def update_config(body: WhitelistConfigBody, request: Request):
    ctx = get_ctx(request)
    return ok(ctx.whitelist.update_config(body.model_dump(exclude_none=True)))


@router.post("/recompute", status_code=202)
def recompute(request: Request, market: str = MARKET_CN_A):
    """手动触发白名单任务：读 instrument_metric 按规则只进不出。"""
    ctx = get_ctx(request)
    payload = {"market": market}
    if request.query_params.get("metric"):
        payload["metric"] = request.query_params["metric"]
    if request.query_params.get("op"):
        payload["op"] = request.query_params["op"]
    if request.query_params.get("value"):
        payload["value"] = request.query_params["value"]
    task_id = ctx.data.submit("whitelist.update", "tushare.whitelist.update", market, payload)
    return ok({"task_id": task_id})


@router.get("/members")
def members(
    request: Request,
    market: str | None = None,
    page_number: int = 1,
    page_size: int = 50,
    keyword: str | None = None,
):
    ctx = get_ctx(request)
    items, total = ctx.whitelist.members(
        market=market,
        page=page_number,
        page_size=page_size,
        keyword=keyword,
    )
    return ok(page(items, total, page_number, page_size))


@router.post("/members")
def add_member(body: MemberBody, request: Request):
    ctx = get_ctx(request)
    market = str(body.market or MARKET_CN_A)
    symbol = (body.symbol or "").strip()
    if not symbol:
        raise ApiError("symbol 不能为空", "5001", 400)
    member = ctx.whitelist.add_member(market, symbol, reason=body.reason)
    return ok(member)


@router.put("/members/{symbol}")
def update_member(symbol: str, body: MemberBody, request: Request, market: str = MARKET_CN_A):
    ctx = get_ctx(request)
    member = ctx.whitelist.update_member(market, symbol, reason=body.reason)
    return ok(member)


@router.delete("/members/{symbol}")
def delete_member(symbol: str, request: Request, market: str = MARKET_CN_A):
    ctx = get_ctx(request)
    ctx.whitelist.delete_member(market, symbol)
    return ok({"deleted": f"{market}:{symbol}"})
