"""白名单（纯集合，只进不出，规则读表）。

- 无 included / enabled：白名单恒为存储范围，空时日 K 任务直接拒绝；
- 规则任务只做 `INSERT ... ON CONFLICT DO UPDATE`（新增/更新指标），
  **不删除、不整表替换、不写落选行**；删条目只有 Web 人工一条路径（记审计）；
- ：规则从 `instrument_metric` 读表（元数据缺失 → 明确失败提示先跑 meta.sync_all），
  不再实时打 `daily_basic`；只进不出语义不变。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from yafip.config import ConfigService
from yafip.core.audit import AuditService
from yafip.core.db import Database, now_iso
from yafip.data.base import DataSource
from yafip.data.market import MarketError, require_market, validate_symbol
from yafip.data.repository import Repository

LATEST_METRIC = "total_mv"
METRIC_LABEL = "最新交易日总市值（万元）"


class WhitelistError(ValueError):
    code = "5001"


class WhitelistMemberExists(WhitelistError):
    http_status = 409


class WhitelistMemberNotFound(WhitelistError):
    http_status = 404


class WhitelistService:
    def __init__(
        self,
        db: Database,
        repo: Repository,
        config: ConfigService,
        audit: AuditService,
        source: DataSource | None = None,
    ):
        self.db = db
        self.repo = repo
        self.config = config
        self.audit = audit
        self.source = source

    # -- 配置 ---------------------------------------------------------------
    def config_view(self, market: str | None = None) -> dict:
        threshold_wan = self.config.get_float("whitelist.threshold_wan", 500000.0)
        return {
            "metric": LATEST_METRIC,
            "metric_label": METRIC_LABEL,
            "threshold_wan": threshold_wan,
            "threshold_yi": round(threshold_wan / 10000.0, 4),
            "members": self.repo.member_count(market),
            "as_of_date": self.repo.whitelist_as_of_date(market),
            "computed_at": self.repo.whitelist_computed_at(market),
        }

    def update_config(self, payload: dict[str, Any], actor: str = "ui") -> dict:
        values: dict[str, str] = {}
        metric = str(payload.get("metric") or LATEST_METRIC)
        if metric != LATEST_METRIC:
            raise WhitelistError("当前仅支持“最新交易日总市值”口径")
        values["whitelist.metric"] = LATEST_METRIC

        threshold: float | None = None
        if payload.get("threshold_wan") is not None:
            threshold = float(payload["threshold_wan"])
        elif payload.get("threshold_yi") is not None:
            threshold = float(payload["threshold_yi"]) * 10000.0
        if threshold is not None:
            if not math.isfinite(threshold) or threshold <= 0:
                raise WhitelistError("门槛必须为大于 0 的有限数字")
            values["whitelist.threshold_wan"] = str(threshold)

        self.config.set_many(values)
        self.audit.write("whitelist.config", "app_config", str(values), actor=actor)
        return self.config_view()

    # -- 规则任务（仅手动触发；按市场；读表 ；只进不出） -------------------
    def recompute_handler(self, ctx, params: dict) -> dict:
        market = require_market(str(params.get("market") or "CN_A"))
        metric = str(params.get("metric") or LATEST_METRIC)
        op = str(params.get("op") or ">")
        value = float(params.get("value") or self.config.get_float("whitelist.threshold_wan", 500000.0))
        if metric != LATEST_METRIC or op != ">":
            raise WhitelistError("本期仅支持 total_mv > value 规则")

        ctx.progress(15, "读取市值指标", f"{market} {metric}")
        rows = self.repo.db.query(
            "SELECT * FROM instrument_metric WHERE market=? AND metric_key=?",
            (market, metric),
        )
        if not rows:
            raise WhitelistError(
                f"{market} 元数据指标为空：请先运行 meta.sync_all 同步元数据与市值，再更新白名单"
            )
        as_of = max(str(r["as_of_date"] or "") for r in rows)
        current = [r for r in rows if str(r["as_of_date"] or "") == as_of]
        stale_days = (date.today() - date.fromisoformat(as_of)).days if as_of else 999
        if stale_days > 7:
            ctx.log("WARN", f"市值指标 as_of={as_of} 距今 {stale_days} 天，建议重新运行 meta.sync_all")
        ctx.checkpoint()

        ctx.progress(55, "计算命中标的", f"{len(current)} 条市值 @ {as_of}")
        hits = [
            r for r in current
            if r["metric_value"] is not None and float(r["metric_value"]) > value
        ]
        ctx.progress(80, "写入白名单（只进不出）", f"命中 {len(hits)}")
        rule_key = f"{metric}_{op}_{int(value)}"
        for r in hits:
            self.repo.upsert_member(
                market, r["symbol"], rule_key=rule_key,
                metric_value=r["metric_value"], as_of_date=as_of,
                reason=None, source="rule",
            )
        ctx.progress(100, "完成", f"{as_of} 命中 {len(hits)} / 总 {len(current)}，只进不出")
        self.audit.write(
            "whitelist.recompute", market,
            f"{as_of} hit={len(hits)} total={len(current)} rule={rule_key}",
        )
        return {
            "market": market,
            "as_of_date": as_of,
            "total_count": len(current),
            "hit_count": len(hits),
            "rule_key": rule_key,
            "threshold_wan": value,
        }

    # -- 成员 CRUD（人工维护；删除唯一路径） --------------------------------
    @staticmethod
    def _normalize(market: str, symbol: str) -> str:
        try:
            return validate_symbol(market, symbol)
        except MarketError as exc:
            raise WhitelistError(f"symbol 格式不合法（{market}）：{exc}") from exc

    def add_member(
        self,
        market: str,
        symbol: str,
        *,
        reason: str | None = None,
        actor: str = "ui",
    ) -> dict:
        code = self._normalize(market, symbol)
        if self.repo.get_member(market, code) is not None:
            raise WhitelistMemberExists(f"标的已存在：{market}:{code}")

        text = (str(reason).strip() if reason is not None else "") or "手动维护"
        self.repo.upsert_member(market, code, reason=text, source="manual")
        self.audit.write("whitelist.member_add", f"{market}:{code}", actor=actor)
        return self.repo.get_member(market, code) or {}

    def update_member(
        self,
        market: str,
        symbol: str,
        *,
        reason: str | None = None,
        actor: str = "ui",
    ) -> dict:
        code = self._normalize(market, symbol)
        current = self.repo.get_member(market, code)
        if current is None:
            raise WhitelistMemberNotFound(f"标的不存在：{market}:{code}")

        new_reason = str(reason).strip() if reason is not None else current.get("reason")
        self.repo.upsert_member(
            market, code,
            rule_key=current.get("rule_key"),
            metric_value=current.get("metric_value"),
            as_of_date=current.get("as_of_date"),
            reason=new_reason or None,
            source=current.get("source") or "manual",
        )
        self.audit.write(
            "whitelist.member_update",
            f"{market}:{code}",
            f"reason={'set' if new_reason else 'cleared'}",
            actor=actor,
        )
        return self.repo.get_member(market, code) or {}

    def delete_member(self, market: str, symbol: str, *, actor: str = "ui") -> None:
        code = self._normalize(market, symbol)
        if not self.repo.delete_member(market, code):
            raise WhitelistMemberNotFound(f"标的不存在：{market}:{code}")
        self.audit.write("whitelist.member_delete", f"{market}:{code}", actor=actor)

    def members(
        self,
        *,
        market: str | None = None,
        page: int = 1,
        page_size: int = 50,
        keyword: str | None = None,
    ) -> tuple[list[dict], int]:
        items, total = self.repo.members(
            market=market, page=page, page_size=page_size, keyword=keyword
        )
        for item in items:
            value = item.get("metric_value")
            item["metric_yi"] = round(float(value) / 10000.0, 2) if value is not None else None
        return items, total

    # -- 抓取侧入口 ---------------------------------------------------------
    def state(self, market: str) -> set[str]:
        """返回该市场的白名单代码集合；空集表示空白名单（抓取应拒绝）。"""
        return set(self.repo.whitelist_codes(market))
