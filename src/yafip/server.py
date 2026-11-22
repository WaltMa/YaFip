"""FastAPI 装配、生命周期、启动入口。

启动：uv run python -m yafip.server（日常使用启动脚本 start_yafip.bat / start_yafip.sh）
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from yafip import __version__, runtime
from yafip.api import (
    routes_dataquery,
    routes_datasource,
    routes_executors,
    routes_fetch,
    routes_overview,
    routes_system,
    routes_task,
    routes_whitelist,
)
from yafip.api.responses import install_error_handlers
from yafip.config import ConfigService
from yafip.context import AppContext
from yafip.core.audit import AuditService
from yafip.core.db import Database
from yafip.core.lifecycle import GracefulStop
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter
from yafip.data.akshare_source import AkshareSource
from yafip.data.executors import build_default_registry
from yafip.data.fetcher import Fetcher
from yafip.data.quality import QualityService
from yafip.data.repository import Repository
from yafip.data.service import DataService, register_handlers
from yafip.data.staging import StagingStore
from yafip.data.tasks.handlers import build_task_handlers
from yafip.data.tushare_source import TushareSource
from yafip.data.whitelist import WhitelistService

logger = logging.getLogger("yafip")


def resolve_root() -> Path:
    return runtime.resolve_root()


def setup_logging(root: Path) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "yafip.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    root_logger.addHandler(console)


def build_context(root: Path) -> AppContext:
    db = Database(root / "data" / "yafip.db")
    # init_schema 内含版本守卫（旧库拒绝）与迁移脚手架
    first_run = db.init_schema()

    config = ConfigService(db)
    config.seed_defaults()

    repo = Repository(db)
    sse = SSEBroker()
    tasks = TaskCenter(db, sse, config=config)
    audit = AuditService(db)
    source = TushareSource(repo, config)
    akshare_source = AkshareSource()
    whitelist = WhitelistService(db, repo, config, audit, source)

    staging = StagingStore(root, db, repo, AuditService(db))
    fetcher = Fetcher(db, repo, config, source, whitelist, staging)
    ak_fetcher = Fetcher(db, repo, config, akshare_source, whitelist, staging)
    quality = QualityService(db, repo, config, whitelist)
    executors = build_default_registry()
    data_service = DataService(tasks, executors, config)
    register_handlers(tasks, fetcher, whitelist, quality)
    # /：任务 handler 按执行器注册表驱动（tushare A股 + akshare 港美股）
    for name, fn in build_task_handlers(
        config=config, repo=repo, source=source, staging=staging,
        fetcher=fetcher, whitelist=whitelist,
        akshare_source=akshare_source, akshare_fetcher=ak_fetcher,
    ).items():
        tasks.register(name, fn)

    # 启动恢复：RUNNING/STOPPING -> FAILED（可重跑，断点保留）
    recovered = tasks.recover()
    if recovered:
        logger.warning("检测到 %s 个中断任务，已标记为 FAILED", recovered)

    logger.info("yafip 启动完成：%s", root)
    return AppContext(
        root=root,
        db=db,
        config=config,
        repo=repo,
        sse=sse,
        tasks=tasks,
        audit=audit,
        source=source,
        akshare_source=akshare_source,
        whitelist=whitelist,
        fetcher=fetcher,
        quality=quality,
        data=data_service,
        executors=executors,
        staging=staging,
    )


ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _hostname_allowed(host: str | None) -> bool:
    """Host 白名单：仅本机回环地址（防 DNS rebinding 攻击）。"""
    if not host:
        return False
    hostname = host.split(":")[0].strip("[]").lower()
    return hostname in ALLOWED_HOSTNAMES


def _origin_matches_host(origin: str, host: str) -> bool:
    """Origin 与 Host 同源即放行；浏览器发起的跨站请求（攻击页面 → 127.0.0.1）
    两者必然不一致。无 Origin 的请求（curl / 脚本 / 旧浏览器简单请求）不拦截，
    因为浏览器对 POST/PUT/DELETE 一律携带 Origin。"""
    if not origin or not host:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return netloc == host


def create_app(root: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = build_context(root)
        ctx.sse.bind_loop(asyncio.get_running_loop())
        app.state.ctx = ctx
        try:
            yield
        finally:
            logger.info("yafip 正在关闭")
            # 先停任务再关连接：shutdown 内部限时等待运行中任务收尾并把残留
            # PENDING/RUNNING 落终态，避免运行线程被跨线程关连接后写库失败。
            wait_sec = ctx.config.get_int("server.stop_wait_sec", 0) or 5.0
            ctx.tasks.shutdown(wait_timeout=wait_sec)
            ctx.db.close_all()

    app = FastAPI(
        title="YaFip",
        version=__version__,
        description="YaFip · 本地量化数据平台（数据底座）",
        lifespan=lifespan,
    )
    install_error_handlers(app)

    # 管理接口无认证（本地单机工具）；至少拦截浏览器跨站写请求（CSRF）。
    # Host 白名单（仅本机回环）防止 DNS rebinding：攻击域名解析到 127.0.0.1 时
    # Host 头是攻击域名，与 Origin 相同也能通过旧的 Origin 校验——这里直接拒绝。
    # POST /system/stop、/cleanup、/vacuum、/tasks/{id}/kill|retry 等均无 body、
    # 绑定 127.0.0.1，第三方页面可轻易构造请求。校验 Host + Origin 为最低成本防线。
    @app.middleware("http")
    async def guard_cross_site_mutations(request: Request, call_next):
        host = request.headers.get("host", "")
        if not _hostname_allowed(host):
            return JSONResponse(
                status_code=403,
                content={"code": 9999, "message": "非法 Host 头，请求被拒绝（仅允许本机 127.0.0.1/localhost）", "data": None},
            )
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and not _origin_matches_host(origin, host):
                return JSONResponse(
                    status_code=403,
                    content={"code": 9999, "message": "跨站写请求被拒绝（Origin 校验失败）", "data": None},
                )
        return await call_next(request)

    for module in (
        routes_overview,
        routes_datasource,
        routes_fetch,
        routes_dataquery,
        routes_whitelist,
        routes_executors,
        routes_task,
        routes_system,
    ):
        app.include_router(module.router)

    web_dir = root / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    app.state.graceful = GracefulStop(app)
    return app


ROOT = resolve_root()
app = create_app(ROOT)


def main() -> int:
    runtime.load_env(ROOT)
    setup_logging(ROOT)
    runtime.migrate_legacy_port(ROOT)
    port = runtime.resolve_port(ROOT)
    runtime.write_run_info(ROOT, port)
    logger.info("启动 yafip：http://127.0.0.1:%s", port)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            # 避免长连接（SSE）拖住退出；任务 drain 已在 GracefulStop 中完成
            timeout_graceful_shutdown=5,
        )
    )
    app.state.server = server
    try:
        server.run()
    finally:
        runtime.clear_run_info(ROOT, os.getpid())
        logger.info("yafip 已退出")
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
