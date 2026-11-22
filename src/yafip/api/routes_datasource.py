from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel

from yafip.api.deps import get_ctx
from yafip.api.responses import ApiError, ok
from yafip.data.registry import SOURCES, SourceMeta, get_source

router = APIRouter(prefix="/api/datasources", tags=["datasource"])

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_api_url(url: str | None) -> None:
    """api_url 白名单：仅 https 或本机 http 调试地址。

    token 会随请求发送到该地址——明文 http 到任意远程主机会导致 token 被
    重定向窃取（攻击者只需把 api_url 改成自己的服务器再等一次抓取）。
    """
    text = str(url or "").strip()
    if not text:
        return
    try:
        parts = urlsplit(text if "://" in text else "https://" + text)
    except ValueError:
        raise ApiError("api_url 不是合法 URL", "1002", 400) from None
    scheme = (parts.scheme or "https").lower()
    hostname = (parts.hostname or "").lower()
    if scheme == "https":
        return  # 官方 / 自定义 https 网关均可（token 走 TLS）
    if scheme == "http" and hostname in _LOCAL_HOSTS:
        return  # 本机调试网关
    raise ApiError(
        "api_url 仅允许 https（或本机 http://127.0.0.1 调试地址），防止 token 被明文发送到远程主机",
        "1002",
        400,
    )


def _mask(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:3]}****{token[-4:]}"


def _require_source(name: str) -> SourceMeta:
    meta = get_source(name)
    if meta is None:
        raise ApiError(f"未知数据源：{name}", "1002", 404)
    return meta


def _serialize(meta: SourceMeta, row: dict | None) -> dict:
    row = row or {}
    token = str(row.get("token") or "")
    return {
        "name": meta.name,
        "title": meta.title,
        "description": meta.description,
        "homepage": meta.homepage,
        "requires_token": meta.requires_token,
        "supports_test": meta.supports_test,
        "implemented": meta.implemented,
        "token_masked": _mask(token),
        "has_token": bool(token),
        "api_url": row.get("api_url"),
        "rpm_limit": row.get("rpm_limit"),
        "enabled": bool(row.get("enabled", 1)),
        "last_test_at": row.get("last_test_at"),
        "last_test_ok": row.get("last_test_ok"),
        "last_test_msg": row.get("last_test_msg"),
    }


class DatasourceBody(BaseModel):
    token: str | None = None
    api_url: str | None = None
    rpm_limit: int | None = None
    enabled: bool | None = None


@router.get("")
def list_datasources(request: Request):
    ctx = get_ctx(request)
    items = [_serialize(meta, ctx.repo.get_datasource(meta.name)) for meta in SOURCES]
    return ok({"items": items})


@router.get("/{name}")
def get_datasource(name: str, request: Request):
    ctx = get_ctx(request)
    meta = _require_source(name)
    return ok(_serialize(meta, ctx.repo.get_datasource(name)))


@router.put("/{name}")
def save_datasource(name: str, body: DatasourceBody, request: Request):
    ctx = get_ctx(request)
    meta = _require_source(name)
    _validate_api_url(body.api_url)
    ctx.repo.save_datasource(
        name,
        token=body.token,
        api_url=body.api_url,
        rpm_limit=body.rpm_limit,
        enabled=body.enabled,
    )
    token_state = "set" if body.token and body.token.strip() else "unchanged"
    if body.api_url is None:
        api_state = "unchanged"
    else:
        api_state = "set" if body.api_url.strip() else "cleared"
    ctx.audit.write(
        "datasource.save",
        name,
        f"rpm={body.rpm_limit} enabled={body.enabled} token={token_state} api_url={api_state}",
    )
    ctx.config.reload()  # 数据源配置存 app_config，写后刷新 ConfigService 缓存
    return ok(_serialize(meta, ctx.repo.get_datasource(name)))


@router.post("/{name}/test")
def test_datasource(name: str, request: Request):
    ctx = get_ctx(request)
    meta = _require_source(name)
    if not meta.implemented or not meta.supports_test:
        raise ApiError(f"{meta.title} 暂不支持连接测试", "1002", 400)
    source = ctx.source if getattr(ctx.source, "name", None) == name else getattr(
        ctx, "akshare_source", None
    )
    if source is None or source.name != name:
        raise ApiError(f"{meta.title} 尚未接入执行器", "1002", 400)
    if meta.requires_token and not ctx.repo.datasource_token(name):
        raise ApiError(f"未配置 {meta.title} token", "1003", 400)
    result = source.test_connection()
    ctx.audit.write("datasource.test", name, "ok" if result.get("ok") else "failed")
    ctx.config.reload()  # test_connection 内部写 last_test_*，刷新缓存
    return ok(result)
