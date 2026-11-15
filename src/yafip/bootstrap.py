"""启动初始化：建库 / 版本守卫 / 迁移脚手架。

不再写入默认白名单（白名单初始为空，由页面或任务维护）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yafip.config import ConfigService
from yafip.core.db import Database
from yafip.data.repository import Repository


@dataclass(frozen=True)
class InitResult:
    status: str  # created / exists / empty
    count: int
    message: str


def initialize(root: Path) -> InitResult:
    """建库 / 版本守卫 / 迁移脚手架；全部幂等；旧库（<v10）由守卫拒绝。"""
    root = Path(root)
    db = Database(root / "data" / "yafip.db")
    try:
        first_run = db.init_schema()
        config = ConfigService(db)
        config.seed_defaults()
        repo = Repository(db)

        if first_run:
            return InitResult(
                "created",
                0,
                "首次创建数据库：全新 schema（user_version=10），白名单为空",
            )
        count = repo.member_count()
        if count > 0:
            return InitResult("exists", count, f"白名单已存在 {count} 只，跳过初始化")
        return InitResult(
            "empty",
            0,
            "白名单为空（可在页面手工新增或运行白名单任务）",
        )
    finally:
        db.close_all()
