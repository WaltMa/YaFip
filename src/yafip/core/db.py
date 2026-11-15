"""SQLite 连接与事务工具。

约束：
- 每个线程持有独立连接，连接对象绝不跨线程共享；
- 自动提交模式（isolation_level=None），事务用 BEGIN IMMEDIATE 显式开启；
- WAL + busy_timeout=30000，允许任务线程与 API 线程并发读写。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 版本守卫：低于该版本的旧库（含业务表）直接拒绝启动（不迁移旧库）
MIN_SCHEMA_VERSION = 10


def now_iso() -> str:
    """本地时区的秒级时间戳，统一存储格式。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class SchemaVersionError(RuntimeError):
    """旧库版本过低：旧库一律归档重建，不迁移。"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()

    # -- 连接 ---------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,  # 自动提交，事务显式控制
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        with self._lock:
            self._conns.append(conn)
        return conn

    def close_all(self) -> None:
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()

    # -- schema -------------------------------------------------------------
    def init_schema(self) -> bool:
        """建库/升级 schema；返回 whitelist_member 是否为本次首次创建。

        流程：读当前 user_version → 旧库（< MIN_SCHEMA_VERSION 且有业务表）
        直接报错提示归档 → 执行 schema.sql（全量权威 DDL，幂等）→
        应用 (current, 目标版本] 区间迁移（脚手架）。
        """
        with self._init_lock:
            conn = self.connect()
            current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            has_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone() is not None
            if current < MIN_SCHEMA_VERSION and has_tables:
                raise SchemaVersionError(
                    f"检测到旧版数据库（user_version={current} < {MIN_SCHEMA_VERSION}）："
                    "旧库一律归档重建，不迁移。请把 data/yafip.db"
                    "（含 -wal/-shm）移到 data/archive/ 后重启，系统将创建全新数据库。"
                )
            first_run = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='whitelist_member'"
                ).fetchone()
                is None
            )
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            from yafip.data.migrations import apply_migrations  # 延迟导入避免循环依赖

            apply_migrations(self, current)
            return first_run

    # -- 查询 ---------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.connect().execute(sql, tuple(params))
        return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        cur = self.connect().executemany(sql, rows)
        return cur.rowcount

    # -- 事务 ---------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        if conn.in_transaction:
            # 嵌套调用复用外层事务，避免 SQLite 的 "cannot start a transaction within a transaction"
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")

    # -- 维护 ---------------------------------------------------------------
    def file_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def vacuum(self) -> None:
        self.connect().execute("VACUUM")

    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check", default="unknown"))
