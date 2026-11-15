"""SQLite 读写（新模型）。

统一 (market, symbol) 二元主键；全部 UPSERT 幂等；日期 ISO。
无 fetch_progress / stock_basic —— 数据覆盖以 bar_* 表为唯一事实源，
增量水位由 sync_state 承担。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import pandas as pd

from yafip.core.db import Database, now_iso
from yafip.data.market import MARKET_CN_A, require_market

NUMERIC_DAILY = ["open", "high", "low", "close", "pre_close", "vol", "amount"]

# 入库侧列契约（executor 边界已把 ts_code 转换为 symbol）
BAR_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "pre_close", "vol", "amount",
]
ADJ_COLUMNS = ["symbol", "trade_date", "adj_factor"]

DEFAULT_DATASET = "bars"
DEFAULT_SCOPE = "WHITELIST"


def _frame_records(df: pd.DataFrame | None, columns: list[str]) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    temp = df.copy()
    for column in columns:
        if column not in temp.columns:
            temp[column] = None
    temp = temp[columns].astype(object).where(pd.notna(temp[columns]), None)
    return temp.to_dict("records")


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ======================================================================
    # 数据源配置（并入 app_config，key 空间 datasource.{name}.{field}）
    # ======================================================================
    @staticmethod
    def _ds_key(name: str, field: str) -> str:
        return f"datasource.{name}.{field}"

    def _ds_get(self, name: str, field: str) -> str | None:
        return self.db.scalar(
            "SELECT value FROM app_config WHERE key=?", (self._ds_key(name, field),)
        )

    def get_datasource(self, name: str = "tushare") -> dict | None:
        rows = self.db.query(
            "SELECT key, value FROM app_config WHERE key LIKE ? ORDER BY key",
            (f"datasource.{name}.%",),
        )
        if not rows:
            return None
        values = {str(r["key"]).rsplit(".", 1)[1]: str(r["value"]) for r in rows}
        rpm_raw = values.get("rpm_limit")
        test_ok_raw = values.get("last_test_ok")
        updated = self.db.scalar(
            "SELECT MAX(updated_at) FROM app_config WHERE key LIKE ?",
            (f"datasource.{name}.%",),
        )
        return {
            "name": name,
            "token": values.get("token", ""),
            "api_url": values.get("api_url"),
            "rpm_limit": int(rpm_raw) if rpm_raw not in (None, "") else None,
            "enabled": 1 if values.get("enabled", "1").strip().lower() in {"1", "true", "yes", "on"} else 0,
            "last_test_at": values.get("last_test_at"),
            "last_test_ok": int(test_ok_raw) if test_ok_raw not in (None, "") else None,
            "last_test_msg": values.get("last_test_msg"),
            "updated_at": updated,
        }

    def save_datasource(
        self,
        name: str,
        *,
        token: str | None = None,
        api_url: str | None = None,
        rpm_limit: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        now = now_iso()
        current = self.get_datasource(name)
        updates: dict[str, str | None] = {}
        if token is not None and token != "":
            updates["token"] = token
        if api_url is not None:
            api_value = str(api_url).strip() or None
            updates["api_url"] = api_value
        if rpm_limit is not None:
            rpm_value = int(rpm_limit) or None
            updates["rpm_limit"] = str(rpm_value) if rpm_value else None
        if enabled is not None:
            updates["enabled"] = "1" if enabled else "0"
        if current is None:
            updates.setdefault("token", "")
            updates.setdefault("enabled", "1")
        if not updates:
            return
        with self.db.tx() as conn:
            for field, value in updates.items():
                key = self._ds_key(name, field)
                if value is None:
                    conn.execute("DELETE FROM app_config WHERE key=?", (key,))
                else:
                    conn.execute(
                        """
                        INSERT INTO app_config(key, value, updated_at) VALUES(?,?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                        """,
                        (key, str(value), now),
                    )

    def set_datasource_test(self, name: str, ok: bool, message: str) -> None:
        now = now_iso()
        with self.db.tx() as conn:
            for field, value in (
                ("last_test_at", now),
                ("last_test_ok", "1" if ok else "0"),
                ("last_test_msg", message[:1000]),
            ):
                conn.execute(
                    """
                    INSERT INTO app_config(key, value, updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (self._ds_key(name, field), value),
                )

    def datasource_token(self, name: str = "tushare") -> str:
        """token 只从本地数据库读取（页面维护），不使用环境变量。"""
        return str(self._ds_get(name, "token") or "")

    def datasource_rpm(self, name: str = "tushare") -> int | None:
        value = self._ds_get(name, "rpm_limit")
        return int(value) if value else None

    # ======================================================================
    # 元数据（全量 instrument）
    # ======================================================================
    def upsert_instrument(self, market: str, records: Iterable[dict], *, source: str) -> int:
        """records 列：symbol/name/asset_type/exchange/currency/list_date/
        delist_date/status/ext_json；market 与 source 由调用方给定。"""
        market = require_market(market)
        now = now_iso()
        rows = []
        for r in records:
            symbol = str(r.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            rows.append(
                (
                    market, symbol,
                    r.get("name"), str(r.get("asset_type") or "STOCK").upper(),
                    r.get("exchange"), r.get("currency"),
                    r.get("list_date"), r.get("delist_date"), r.get("status"),
                    r.get("ext_json"), source, now,
                )
            )
        if not rows:
            return 0
        return self.db.executemany(
            """
            INSERT INTO instrument(
                market, symbol, name, asset_type, exchange, currency,
                list_date, delist_date, status, ext_json, source, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market, symbol) DO UPDATE SET
                name=excluded.name, asset_type=excluded.asset_type,
                exchange=excluded.exchange, currency=excluded.currency,
                list_date=excluded.list_date, delist_date=excluded.delist_date,
                status=excluded.status, ext_json=excluded.ext_json,
                source=excluded.source, updated_at=excluded.updated_at
            """,
            rows,
        )

    def upsert_instrument_metric(
        self,
        market: str,
        symbol: str,
        metric_key: str,
        metric_value: float | None,
        *,
        unit: str | None = None,
        as_of_date: str | None = None,
        source: str | None = None,
    ) -> None:
        market = require_market(market)
        self.db.execute(
            """
            INSERT INTO instrument_metric(
                market, symbol, metric_key, metric_value, unit, as_of_date, source, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(market, symbol, metric_key) DO UPDATE SET
                metric_value=excluded.metric_value, unit=excluded.unit,
                as_of_date=excluded.as_of_date, source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (market, symbol, metric_key, metric_value, unit, as_of_date, source, now_iso()),
        )

    def get_instrument(self, market: str, symbol: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM instrument WHERE market=? AND symbol=?",
            (require_market(market), symbol),
        )
        return dict(row) if row else None

    def instrument_count(self, market: str | None = None) -> int:
        if market:
            return int(self.db.scalar("SELECT COUNT(*) FROM instrument WHERE market=?", (market,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM instrument", default=0))

    def instruments(
        self,
        *,
        market: str | None = None,
        keyword: str | None = None,
        asset_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if market:
            where.append("market=?")
            params.append(market)
        if asset_type:
            where.append("asset_type=?")
            params.append(asset_type)
        if keyword:
            where.append("(symbol LIKE ? OR name LIKE ?)")
            like = f"%{keyword.strip()}%"
            params.extend([like, like])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM instrument {clause}", params, 0))
        page = max(1, page)
        page_size = max(1, min(500, page_size))
        rows = self.db.query(
            f"""
            SELECT market, symbol, name, asset_type, exchange, currency,
                   list_date, delist_date, status, ext_json, source, updated_at
            FROM instrument {clause}
            ORDER BY market, symbol
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        )
        return [dict(r) for r in rows], total

    # ======================================================================
    # 交易日历（market 维度）
    # ======================================================================
    def upsert_calendar(self, market: str, records: Iterable[dict]) -> int:
        market = require_market(market)
        rows = [
            (market, r["cal_date"], int(r["is_open"] or 0), r.get("pretrade_date"))
            for r in records
            if r.get("cal_date")
        ]
        if not rows:
            return 0
        return self.db.executemany(
            """
            INSERT INTO trade_calendar(market, cal_date, is_open, pretrade_date)
            VALUES(?,?,?,?)
            ON CONFLICT(market, cal_date) DO UPDATE SET
                is_open=excluded.is_open, pretrade_date=excluded.pretrade_date
            """,
            rows,
        )

    def calendar_count(self, market: str | None = None) -> int:
        if market:
            return int(self.db.scalar("SELECT COUNT(*) FROM trade_calendar WHERE market=?", (market,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM trade_calendar", default=0))

    def open_dates(self, market: str, start: str, end: str) -> list[str]:
        rows = self.db.query(
            """
            SELECT cal_date FROM trade_calendar
            WHERE market=? AND is_open=1 AND cal_date BETWEEN ? AND ?
            ORDER BY cal_date
            """,
            (require_market(market), start, end),
        )
        return [str(r["cal_date"]) for r in rows]

    def latest_open_date(self, market: str, on_or_before: str) -> str | None:
        return self.db.scalar(
            """
            SELECT MAX(cal_date) FROM trade_calendar
            WHERE market=? AND is_open=1 AND cal_date<=?
            """,
            (require_market(market), on_or_before),
        )

    def is_open(self, market: str, day: str) -> int | None:
        value = self.db.scalar(
            "SELECT is_open FROM trade_calendar WHERE market=? AND cal_date=?",
            (require_market(market), day),
        )
        return int(value) if value is not None else None

    # ======================================================================
    # 行情写入（bar_daily / bar_adj_factor）
    # ======================================================================
    def upsert_bars(
        self,
        market: str,
        df: pd.DataFrame | None,
        *,
        source: str,
        currency: str | None = None,
        vol_unit: str | None = None,
        amount_unit: str | None = None,
    ) -> int:
        """df 列：symbol/trade_date/open/high/low/close/pre_close/vol/amount。"""
        market = require_market(market)
        records = _frame_records(df, BAR_COLUMNS)
        rows = [
            (
                market, r["symbol"], r["trade_date"], r["open"], r["high"], r["low"],
                r["close"], r["pre_close"], r["vol"], r["amount"],
                currency, vol_unit, amount_unit, source,
            )
            for r in records
            if r["symbol"] and r["trade_date"]
        ]
        if not rows:
            return 0
        return self.db.executemany(
            """
            INSERT INTO bar_daily(
                market, symbol, trade_date, open, high, low, close, pre_close,
                vol, amount, currency, vol_unit, amount_unit, source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market, symbol, trade_date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, pre_close=excluded.pre_close,
                vol=excluded.vol, amount=excluded.amount,
                currency=excluded.currency, vol_unit=excluded.vol_unit,
                amount_unit=excluded.amount_unit, source=excluded.source
            """,
            rows,
        )

    def upsert_adj(
        self,
        market: str,
        df: pd.DataFrame | None,
        *,
        source: str,
        adj_type: str = "FACTOR",
    ) -> int:
        """df 列：symbol/trade_date/adj_factor。"""
        market = require_market(market)
        records = _frame_records(df, ADJ_COLUMNS)
        rows = [
            (market, r["symbol"], r["trade_date"], float(r["adj_factor"]), adj_type)
            for r in records
            if r["symbol"] and r["trade_date"] and r["adj_factor"] is not None
        ]
        if not rows:
            return 0
        return self.db.executemany(
            """
            INSERT INTO bar_adj_factor(market, symbol, trade_date, adj_factor, adj_type)
            VALUES(?,?,?,?,?)
            ON CONFLICT(market, symbol, trade_date) DO UPDATE SET
                adj_factor=excluded.adj_factor, adj_type=excluded.adj_type
            """,
            rows,
        )

    # ======================================================================
    # 查询（复权 CTE 沿用旧算法，仅加 market 维度）
    # ======================================================================
    def daily_query(
        self,
        market: str,
        symbol: str,
        start: str,
        end: str,
        price_type: str = "raw",
        limit: int = 5000,
    ) -> list[dict]:
        market = require_market(market)
        limit = max(1, min(20000, limit))
        if price_type in ("qfq", "hfq"):
            denominator = "COALESCE(anchor.f, 1.0)" if price_type == "qfq" else "1.0"
            sql = f"""
            WITH anchor AS (
                SELECT adj_factor AS f
                FROM bar_adj_factor
                WHERE market = ? AND symbol = ?
                ORDER BY trade_date DESC LIMIT 1
            )
            SELECT d.trade_date,
                   d.open       * COALESCE(a.adj_factor, anchor.f, 1.0) / {denominator} AS open,
                   d.high       * COALESCE(a.adj_factor, anchor.f, 1.0) / {denominator} AS high,
                   d.low        * COALESCE(a.adj_factor, anchor.f, 1.0) / {denominator} AS low,
                   d.close      * COALESCE(a.adj_factor, anchor.f, 1.0) / {denominator} AS close,
                   d.pre_close  * COALESCE(a.adj_factor, anchor.f, 1.0) / {denominator} AS pre_close,
                   (d.close - d.pre_close) * COALESCE(a.adj_factor, anchor.f, 1.0) / {denominator} AS change,
                   CASE WHEN d.pre_close IS NULL OR d.pre_close = 0 THEN NULL
                        ELSE ROUND((d.close - d.pre_close) * 100.0 / d.pre_close, 2)
                   END AS pct_chg,
                   d.vol, d.amount, COALESCE(a.adj_factor, anchor.f, 1.0) AS adj_factor
            FROM bar_daily d
            LEFT JOIN bar_adj_factor a
              ON a.market = d.market AND a.symbol = d.symbol AND a.trade_date = d.trade_date
            LEFT JOIN anchor ON 1 = 1
            WHERE d.market = ? AND d.symbol = ? AND d.trade_date BETWEEN ? AND ?
            ORDER BY d.trade_date LIMIT ?
            """
            params = (market, symbol, market, symbol, start, end, limit)
        else:
            sql = """
            SELECT d.trade_date, d.open, d.high, d.low, d.close, d.pre_close,
                   d.close - d.pre_close AS change,
                   CASE WHEN d.pre_close IS NULL OR d.pre_close = 0 THEN NULL
                        ELSE ROUND((d.close - d.pre_close) * 100.0 / d.pre_close, 2)
                   END AS pct_chg,
                   d.vol, d.amount, a.adj_factor
            FROM bar_daily d
            LEFT JOIN bar_adj_factor a
              ON a.market = d.market AND a.symbol = d.symbol AND a.trade_date = d.trade_date
            WHERE d.market = ? AND d.symbol = ? AND d.trade_date BETWEEN ? AND ?
            ORDER BY d.trade_date LIMIT ?
            """
            params = (market, symbol, start, end, limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def bar_count(self, market: str | None = None) -> int:
        if market:
            return int(self.db.scalar("SELECT COUNT(*) FROM bar_daily WHERE market=?", (market,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM bar_daily", default=0))

    def adj_count(self, market: str | None = None) -> int:
        if market:
            return int(self.db.scalar("SELECT COUNT(*) FROM bar_adj_factor WHERE market=?", (market,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM bar_adj_factor", default=0))

    def count_bars_on(self, market: str, day: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM bar_daily WHERE market=? AND trade_date=?", (market, day), 0))

    def distinct_bar_dates(self, market: str | None = None) -> list[str]:
        if market:
            rows = self.db.query("SELECT DISTINCT trade_date FROM bar_daily WHERE market=? ORDER BY trade_date", (market,))
        else:
            rows = self.db.query("SELECT DISTINCT trade_date FROM bar_daily ORDER BY trade_date")
        return [str(r["trade_date"]) for r in rows]

    def coverage(self, market: str, granularity: str = "year") -> list[dict]:
        length = 4 if granularity == "year" else 7
        rows = self.db.query(
            """
            SELECT substr(trade_date, 1, ?) AS bucket,
                   COUNT(*) AS rows,
                   COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS first_date,
                   MAX(trade_date) AS last_date
            FROM bar_daily
            WHERE market=?
            GROUP BY bucket
            ORDER BY bucket DESC
            """,
            (length, market),
        )
        return [dict(r) for r in rows]

    def latest_bar_date(self, market: str | None = None) -> str | None:
        if market:
            return self.db.scalar("SELECT MAX(trade_date) FROM bar_daily WHERE market=?", (market,))
        return self.db.scalar("SELECT MAX(trade_date) FROM bar_daily")

    # ======================================================================
    # 同步水位（sync_state；替代 fetch_progress）
    # ======================================================================
    def get_sync_state(self, market: str, dataset: str = DEFAULT_DATASET, scope: str = DEFAULT_SCOPE) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM sync_state WHERE market=? AND dataset=? AND scope=?",
            (require_market(market), dataset, scope),
        )
        return dict(row) if row else None

    def set_sync_state(
        self,
        market: str,
        last_trade_date: str | None,
        *,
        dataset: str = DEFAULT_DATASET,
        scope: str = DEFAULT_SCOPE,
        status: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO sync_state(market, dataset, scope, last_trade_date, last_success_at, status)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(market, dataset, scope) DO UPDATE SET
                last_trade_date=excluded.last_trade_date,
                last_success_at=excluded.last_success_at,
                status=excluded.status
            """,
            (require_market(market), dataset, scope, last_trade_date, now_iso() if last_trade_date else None, status),
        )

    def sync_state_watermark(self, market: str, dataset: str = DEFAULT_DATASET, scope: str = DEFAULT_SCOPE) -> str | None:
        state = self.get_sync_state(market, dataset, scope)
        return state.get("last_trade_date") if state else None

    # ======================================================================
    # 白名单（纯集合：只进不出，无 included；删除只能人工）
    # ======================================================================
    def upsert_member(
        self,
        market: str,
        symbol: str,
        *,
        rule_key: str | None = None,
        metric_value: float | None = None,
        as_of_date: str | None = None,
        reason: str | None = None,
        source: str = "manual",
        computed_at: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO whitelist_member(
                market, symbol, rule_key, metric_value, as_of_date, reason, source, computed_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(market, symbol) DO UPDATE SET
                rule_key=excluded.rule_key, metric_value=excluded.metric_value,
                as_of_date=excluded.as_of_date, reason=excluded.reason,
                source=excluded.source, computed_at=excluded.computed_at
            """,
            (
                require_market(market), symbol, rule_key, metric_value, as_of_date,
                reason, source, computed_at or now_iso(),
            ),
        )

    def get_member(self, market: str, symbol: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM whitelist_member WHERE market=? AND symbol=?",
            (require_market(market), symbol),
        )
        return dict(row) if row else None

    def delete_member(self, market: str, symbol: str) -> int:
        return self.db.execute(
            "DELETE FROM whitelist_member WHERE market=? AND symbol=?",
            (require_market(market), symbol),
        )

    def member_count(self, market: str | None = None) -> int:
        if market:
            return int(self.db.scalar("SELECT COUNT(*) FROM whitelist_member WHERE market=?", (market,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM whitelist_member", default=0))

    def whitelist_codes(self, market: str) -> list[str]:
        rows = self.db.query(
            "SELECT symbol FROM whitelist_member WHERE market=? ORDER BY symbol",
            (require_market(market),),
        )
        return [str(r["symbol"]) for r in rows]

    def members(
        self,
        *,
        market: str | None = None,
        page: int = 1,
        page_size: int = 50,
        keyword: str | None = None,
    ) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if market:
            where.append("m.market=?")
            params.append(market)
        if keyword:
            where.append("(m.symbol LIKE ? OR COALESCE(i.name,'') LIKE ?)")
            like = f"%{keyword.strip()}%"
            params.extend([like, like])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        base = """
            FROM whitelist_member m
            LEFT JOIN instrument i ON i.market=m.market AND i.symbol=m.symbol
        """
        total = int(self.db.scalar(f"SELECT COUNT(*) {base} {clause}", params, 0))
        page = max(1, page)
        page_size = max(1, min(500, page_size))
        rows = self.db.query(
            f"""
            SELECT m.market, m.symbol, i.name, i.asset_type, i.status,
                   m.rule_key, m.metric_value, m.as_of_date, m.reason, m.source, m.computed_at
            {base}
            {clause}
            ORDER BY m.market, m.symbol
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        )
        return [dict(r) for r in rows], total

    def whitelist_as_of_date(self, market: str | None = None) -> str | None:
        if market:
            return self.db.scalar("SELECT MAX(as_of_date) FROM whitelist_member WHERE market=?", (market,))
        return self.db.scalar("SELECT MAX(as_of_date) FROM whitelist_member")

    def whitelist_computed_at(self, market: str | None = None) -> str | None:
        if market:
            return self.db.scalar("SELECT MAX(computed_at) FROM whitelist_member WHERE market=?", (market,))
        return self.db.scalar("SELECT MAX(computed_at) FROM whitelist_member")

    # ======================================================================
    # 质量（market 维度）
    # ======================================================================
    def mark_scan_start(self, check_type: str, market: str, start: str | None = None, end: str | None = None) -> None:
        if start and end:
            self.db.execute(
                """
                UPDATE data_quality_issue SET detected_at = ''
                WHERE check_type=? AND market=? AND resolved=0
                  AND (trade_date = '' OR trade_date BETWEEN ? AND ?)
                """,
                (check_type, market, start, end),
            )
        else:
            self.db.execute(
                "UPDATE data_quality_issue SET detected_at='' WHERE check_type=? AND market=? AND resolved=0",
                (check_type, market),
            )

    def insert_issues(self, issues: Iterable[dict]) -> int:
        rows = [
            (
                i["check_type"], i.get("market") or "", i.get("symbol") or "", i.get("trade_date") or "",
                i.get("detail"), now_iso(), 0,
            )
            for i in issues
        ]
        if not rows:
            return 0
        return self.db.executemany(
            """
            INSERT INTO data_quality_issue(check_type, market, symbol, trade_date, detail, detected_at, resolved)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(check_type, market, symbol, trade_date) DO UPDATE SET
                detail = excluded.detail,
                detected_at = excluded.detected_at,
                resolved = excluded.resolved,
                resolved_at = NULL
            """,
            rows,
        )

    def purge_stale_issues(
        self,
        check_type: str,
        market: str,
        start: str | None = None,
        end: str | None = None,
    ) -> int:
        where = ["resolved = 0", "detected_at = ''", "check_type = ?", "market = ?"]
        params: list[Any] = [check_type, market]
        if start and end:
            where.append("(trade_date = '' OR trade_date BETWEEN ? AND ?)")
            params.extend([start, end])
        return self.db.execute("DELETE FROM data_quality_issue WHERE " + " AND ".join(where), params)

    def issues(
        self,
        *,
        check_type: str | None = None,
        market: str | None = None,
        resolved: int | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if check_type:
            where.append("check_type=?")
            params.append(check_type)
        if market:
            where.append("market=?")
            params.append(market)
        if resolved is not None:
            where.append("resolved=?")
            params.append(resolved)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM data_quality_issue {clause}", params, 0))
        page = max(1, page)
        page_size = max(1, min(500, page_size))
        rows = self.db.query(
            f"SELECT * FROM data_quality_issue {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )
        return [dict(r) for r in rows], total

    def unresolved_issue_count(self, market: str | None = None) -> int:
        if market:
            return int(self.db.scalar("SELECT COUNT(*) FROM data_quality_issue WHERE resolved=0 AND market=?", (market,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM data_quality_issue WHERE resolved=0", default=0))

    def resolve_issues(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        return self.db.execute(
            f"UPDATE data_quality_issue SET resolved=1, resolved_at=? WHERE id IN ({placeholders})",
            [now_iso(), *list(ids)],
        )

    def cleanup_resolved_issues(self, cutoff: str) -> int:
        return self.db.execute(
            """
            DELETE FROM data_quality_issue
            WHERE resolved=1
              AND COALESCE(NULLIF(resolved_at,''), NULLIF(detected_at,'')) < ?
            """,
            (cutoff,),
        )

    # ======================================================================
    # 概览
    # ======================================================================
    def overview_stats(self) -> dict:
        return {
            "instruments": self.instrument_count(),
            "bar_rows": self.bar_count(),
            "bar_dates": int(self.db.scalar("SELECT COUNT(DISTINCT trade_date) FROM bar_daily", default=0)),
            "adj_rows": self.adj_count(),
            "latest_bar_date": self.latest_bar_date(),
            "db_bytes": self.db.file_size(),
            "quality_open": self.unresolved_issue_count(),
            "whitelist": {
                "members": self.member_count(),
                "as_of_date": self.whitelist_as_of_date(),
                "computed_at": self.whitelist_computed_at(),
            },
        }
