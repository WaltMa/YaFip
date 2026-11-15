"""数据库迁移：最小版本化框架。

只保留机制：旧库一律归档重建（由 core/db.py 的 MIN_SCHEMA_VERSION
版本守卫拒绝并提示归档，不做历史数据搬迁）。

约定：
- schema.sql 是全量权威 DDL，末尾声明 PRAGMA user_version = <目标版本>；
- init_schema() 先读当前 user_version（< MIN_SCHEMA_VERSION 且有业务表
  → 报错提示归档），再执行 schema.sql，最后按序应用 MIGRATIONS 中
  version > 当前版本的迁移；
- 每个迁移必须幂等；失败即抛，启动终止（宁可启动失败也不留半迁移状态）。
"""
from __future__ import annotations

from typing import Callable

from yafip.core.db import Database


def migrate_base_v10(db: Database) -> None:
    """基线占位迁移：新库初始化。

    schema.sql 已建全表并将 user_version 置为 10，本迁移仅在异常路径兜底
    （例如手工改过版本号）时把版本推进到 10。
    """
    conn = db.connect()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 10:
        conn.execute("PRAGMA user_version = 10")


MIGRATIONS: list[tuple[int, str, Callable[[Database], None]]] = [
    (10, "migrate_base_v10", migrate_base_v10),
]


def apply_migrations(db: Database, current_version: int) -> int:
    """应用 (current_version, 目标版本] 区间内的迁移；返回应用个数。"""
    applied = 0
    for version, _name, fn in sorted(MIGRATIONS):
        if version > current_version:
            fn(db)
            applied += 1
    return applied
