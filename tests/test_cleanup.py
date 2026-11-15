from __future__ import annotations

from conftest import make_db

from yafip.data.repository import Repository


def test_cleanup_resolved_issues_only_old(tmp_path):
    db = make_db(tmp_path)
    repo = Repository(db)
    try:
        db.execute(
            "INSERT INTO data_quality_issue(check_type, market, symbol, trade_date, detail, detected_at, resolved, resolved_at) "
            "VALUES('adj_missing','CN_A','600519.SH','2025-01-01','x','2025-01-01T00:00:00',1,'2025-01-02T00:00:00')"
        )
        db.execute(
            "INSERT INTO data_quality_issue(check_type, market, symbol, trade_date, detail, detected_at, resolved, resolved_at) "
            "VALUES('adj_missing','CN_A','000001.SZ','2025-01-01','x','2026-01-01T00:00:00',1,'2026-01-02T00:00:00')"
        )
        db.execute(
            "INSERT INTO data_quality_issue(check_type, market, symbol, trade_date, detail, detected_at, resolved) "
            "VALUES('adj_missing','CN_A','000002.SZ','2025-01-01','x','2025-01-01T00:00:00',0)"
        )

        deleted = repo.cleanup_resolved_issues("2026-01-01T00:00:00")
        assert deleted == 1
        remaining = {
            row["symbol"] for row in db.query("SELECT symbol FROM data_quality_issue")
        }
        assert remaining == {"000001.SZ", "000002.SZ"}
    finally:
        db.close_all()
