"""数据质量校验：市场维度 + bar_*/instrument 新表。

缺失日判定以 instrument.list_date / delist_date 为界。
"""
from __future__ import annotations

import bisect
from datetime import date, timedelta
from typing import Any

from yafip.config import ConfigService
from yafip.core.db import Database
from yafip.data.market import MARKET_CN_A, require_market
from yafip.data.repository import Repository
from yafip.data.whitelist import WhitelistService

MAX_ISSUES_PER_CHECK = 2000


class QualityService:
    def __init__(
        self,
        db: Database,
        repo: Repository,
        config: ConfigService,
        whitelist: WhitelistService,
    ):
        self.db = db
        self.repo = repo
        self.config = config
        self.whitelist = whitelist

    def scan(self, ctx, params: dict[str, Any]) -> dict:
        market = require_market(str(params.get("market") or MARKET_CN_A))
        today = date.today().isoformat()
        start = str(params.get("start") or (date.today() - timedelta(days=365)).isoformat())[:10]
        end = str(params.get("end") or today)[:10]
        symbols = [str(s) for s in (params.get("symbols") or []) if str(s)]
        if not symbols:
            symbols = sorted(self.whitelist.state(market))

        ctx.progress(3, "标记本轮检查", market)
        for check_type in ("invalid_ohlc", "adj_missing", "missing_days", "row_count_anomaly"):
            self.repo.mark_scan_start(check_type, market, start, end)

        collected: list[dict] = []
        summary: dict[str, int] = {}

        ctx.progress(10, "检查异常行情")
        invalid = self._invalid_rows(market, start, end, symbols)
        collected.extend(invalid)
        summary["invalid_ohlc"] = len(invalid)
        ctx.checkpoint()

        ctx.progress(28, "检查复权因子")
        adj_missing = self._adj_missing(market, start, end, symbols)
        collected.extend(adj_missing)
        summary["adj_missing"] = len(adj_missing)
        ctx.checkpoint()

        ctx.progress(48, "检查缺失交易日")
        missing = self._missing_days(market, start, end, symbols)
        collected.extend(missing)
        summary["missing_days"] = len(missing)
        ctx.checkpoint()

        ctx.progress(72, "检查单日行数")
        anomalies = self._row_count_anomaly(market, start, end)
        collected.extend(anomalies)
        summary["row_count_anomaly"] = len(anomalies)
        ctx.checkpoint()

        ctx.progress(85, "写入结果")
        if len(collected) > MAX_ISSUES_PER_CHECK * 4:
            collected = collected[: MAX_ISSUES_PER_CHECK * 4]
        written = self.repo.insert_issues(collected)
        removed = 0
        for check_type in ("invalid_ohlc", "adj_missing", "missing_days", "row_count_anomaly"):
            removed += self.repo.purge_stale_issues(check_type, market, start, end)
        ctx.progress(100, "完成", f"新增 {written} 条，清除未复现 {removed} 条")
        ctx.log("INFO", f"质量扫描完成：{market} {summary}")
        return {
            "market": market,
            "range": {"start": start, "end": end},
            "counts": summary,
            "written": written,
            "removed": removed,
        }

    # ----------------------------------------------------------------------
    def _invalid_rows(self, market: str, start: str, end: str, symbols: list[str]) -> list[dict]:
        where = [
            "market = ?",
            "trade_date BETWEEN ? AND ?",
            "(high < low OR close <= 0 OR open <= 0 OR low <= 0 OR vol < 0)",
        ]
        params: list[Any] = [market, start, end]
        if symbols:
            where.append(f"symbol IN ({','.join('?' for _ in symbols)})")
            params.extend(symbols)
        rows = self.db.query(
            f"""
            SELECT symbol, trade_date, open, high, low, close, vol
            FROM bar_daily
            WHERE {' AND '.join(where)}
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [*params, MAX_ISSUES_PER_CHECK],
        )
        return [
            {
                "check_type": "invalid_ohlc",
                "market": market,
                "symbol": r["symbol"],
                "trade_date": r["trade_date"],
                "detail": (
                    f"open={r['open']} high={r['high']} low={r['low']} "
                    f"close={r['close']} vol={r['vol']}"
                ),
            }
            for r in rows
        ]

    def _adj_missing(self, market: str, start: str, end: str, symbols: list[str]) -> list[dict]:
        where = [
            "d.market = ?",
            "d.trade_date BETWEEN ? AND ?",
            "(a.adj_factor IS NULL OR a.adj_factor <= 0)",
        ]
        params: list[Any] = [market, start, end]
        if symbols:
            where.append(f"d.symbol IN ({','.join('?' for _ in symbols)})")
            params.extend(symbols)
        rows = self.db.query(
            f"""
            SELECT d.symbol, d.trade_date, a.adj_factor
            FROM bar_daily d
            LEFT JOIN bar_adj_factor a
              ON a.market=d.market AND a.symbol=d.symbol AND a.trade_date=d.trade_date
            WHERE {' AND '.join(where)}
            ORDER BY d.trade_date DESC
            LIMIT ?
            """,
            [*params, MAX_ISSUES_PER_CHECK],
        )
        return [
            {
                "check_type": "adj_missing",
                "market": market,
                "symbol": r["symbol"],
                "trade_date": r["trade_date"],
                "detail": f"adj_factor={r['adj_factor']}",
            }
            for r in rows
        ]

    def _missing_days(self, market: str, start: str, end: str, symbols: list[str]) -> list[dict]:
        open_dates = self.repo.open_dates(market, start, end)
        if not open_dates:
            return []

        where = ["i.market = ?"]
        # 注意占位符顺序：JOIN 的 d.trade_date BETWEEN ? AND ? 在前，WHERE i.market = ? 在后
        params: list[Any] = [start, end, market]
        if symbols:
            where.append(f"i.symbol IN ({','.join('?' for _ in symbols)})")
            params.extend(symbols)

        rows = self.db.query(
            f"""
            SELECT i.symbol, i.list_date, i.delist_date, COUNT(d.trade_date) AS actual
            FROM instrument i
            LEFT JOIN bar_daily d
              ON d.market=i.market AND d.symbol=i.symbol AND d.trade_date BETWEEN ? AND ?
            WHERE {' AND '.join(where)}
            GROUP BY i.symbol, i.list_date, i.delist_date
            """,
            params,
        )

        issues: list[dict] = []
        for row in rows:
            list_date = row["list_date"] or start
            delist_date = row["delist_date"] or end
            lo = max(str(list_date)[:10], start)
            hi = min(str(delist_date)[:10], end)
            if lo > hi:
                continue
            expected = bisect.bisect_right(open_dates, hi) - bisect.bisect_left(open_dates, lo)
            if expected <= 0:
                continue
            actual = int(row["actual"] or 0)
            if actual >= expected:
                continue
            issues.append(
                {
                    "check_type": "missing_days",
                    "market": market,
                    "symbol": row["symbol"],
                    "trade_date": "",
                    "detail": f"missing={expected - actual}/{expected}（{lo}~{hi}，可能含停牌）",
                }
            )
            if len(issues) >= MAX_ISSUES_PER_CHECK:
                break
        return issues

    def _row_count_anomaly(self, market: str, start: str, end: str) -> list[dict]:
        rows = self.db.query(
            """
            SELECT trade_date, COUNT(*) AS n
            FROM bar_daily
            WHERE market = ? AND trade_date BETWEEN ? AND ?
            GROUP BY trade_date
            ORDER BY trade_date
            """,
            (market, start, end),
        )
        counts = [int(r["n"]) for r in rows]
        if len(counts) < 5:
            return []
        ordered = sorted(counts)
        median = ordered[len(ordered) // 2]
        return [
            {
                "check_type": "row_count_anomaly",
                "market": market,
                "symbol": "",
                "trade_date": r["trade_date"],
                "detail": f"rows={r['n']} median={median}",
            }
            for r in rows
            if int(r["n"]) < median * 0.5
        ][:MAX_ISSUES_PER_CHECK]

    # ----------------------------------------------------------------------
    def missing_dates_for(self, market: str, symbol: str, start: str, end: str) -> list[str]:
        instrument = self.repo.get_instrument(market, symbol)
        if instrument is None:
            return []
        lo = max(str(instrument.get("list_date") or start)[:10], start)
        hi = min(str(instrument.get("delist_date") or end)[:10], end)
        if lo > hi:
            return []
        expected = self.repo.open_dates(market, lo, hi)
        rows = self.db.query(
            "SELECT trade_date FROM bar_daily WHERE market=? AND symbol=? AND trade_date BETWEEN ? AND ?",
            (market, symbol, lo, hi),
        )
        actual = {str(r["trade_date"]) for r in rows}
        return [d for d in expected if d not in actual]
