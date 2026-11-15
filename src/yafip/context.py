"""应用上下文：装配顺序在 server.py，路由通过 request.app.state.ctx 访问。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.core.db import Database
from yafip.core.sse import SSEBroker
from yafip.core.task_center import TaskCenter
from yafip.data.akshare_source import AkshareSource
from yafip.data.base import DataSource
from yafip.data.executors import ExecutorRegistry
from yafip.data.fetcher import Fetcher
from yafip.data.quality import QualityService
from yafip.data.repository import Repository
from yafip.data.service import DataService
from yafip.data.staging import StagingStore
from yafip.data.whitelist import WhitelistService


@dataclass
class AppContext:
    root: Path
    db: Database
    config: ConfigService
    repo: Repository
    sse: SSEBroker
    tasks: TaskCenter
    audit: AuditService
    source: DataSource
    akshare_source: AkshareSource
    whitelist: WhitelistService
    fetcher: Fetcher
    quality: QualityService
    data: DataService
    executors: ExecutorRegistry
    staging: StagingStore
