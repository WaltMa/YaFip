"""操作审计。所有危险操作写一行。"""
from __future__ import annotations

from typing import Any

from yafip.core.db import Database, now_iso


class AuditService:
    def __init__(self, db: Database):
        self.db = db

    def write(self, action: str, target: str = "", detail: Any = "", actor: str = "ui") -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts, actor, action, target, detail) VALUES(?,?,?,?,?)",
            (now_iso(), actor, action, target, "" if detail is None else str(detail)),
        )

    def list(self, page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM audit_log", default=0))
        page = max(1, page)
        page_size = max(1, min(500, page_size))
        rows = self.db.query(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        )
        return [dict(r) for r in rows], total
