"""统一响应与错误码。

成功：{"code": 0, "message": "ok", "data": ...}
失败：{"code": 2002, "message": "...", "data": null}
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from yafip.config import ConfigError
from yafip.core.task_center import TaskError
from yafip.data.base import DataSourceError
from yafip.data.executors import ExecutorError
from yafip.data.fetcher import FetchParamError
from yafip.data.market import MarketError
from yafip.data.whitelist import WhitelistError

ERRORS = {
    "1001": "CONFIG_NOT_FOUND",
    "1002": "DATASOURCE_UNAVAILABLE",
    "1003": "TOKEN_INVALID",
    "2001": "TASK_NOT_FOUND",
    "2002": "TASK_CONFLICT",
    "2003": "TASK_NOT_KILLABLE",
    "2004": "TASK_NOT_RETRYABLE",
    "3001": "EXECUTOR_INVALID",
    "4001": "DATA_QUERY_RANGE_TOO_LARGE",
    "5001": "WHITELIST_INVALID",
    "5002": "MV_UNIT_MISMATCH",
    "6001": "MARKET_INVALID",
}


class ApiError(Exception):
    def __init__(self, message: str, code: str = "9999", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def ok(data: Any = None) -> dict:
    return {"code": 0, "message": "ok", "data": data}


def page(items: list, total: int, page_number: int, page_size: int) -> dict:
    return {
        "items": items,
        "total": total,
        "page": page_number,
        "page_size": page_size,
    }


def _code_int(code: Any) -> Any:
    text = str(code)
    return int(text) if text.isdigit() else code


async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    code = _code_int(getattr(exc, "code", "9999"))
    status = int(getattr(exc, "http_status", 400))
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": str(exc), "data": None},
    )


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "code": 9999,
            "message": f"{type(exc).__name__}: {exc}",
            "data": None,
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    for cls in (
        ApiError,
        ConfigError,
        TaskError,
        DataSourceError,
        ExecutorError,
        FetchParamError,
        MarketError,
        WhitelistError,
    ):
        app.add_exception_handler(cls, domain_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
