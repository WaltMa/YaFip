"""StagingStore：staging → validate → commit 三段式入库。

- 文件本体：`data/staging/{dataset}/{market}/{batch_id}/`，每单元一个 CSV，
  同目录一份 `manifest.json`；
- 状态机：created → validated → applied → archived；任一环节失败置 failed 并保留文件；
- commit()：单事务批量 upsert + 写 staging_batch(applied)，失败整体回滚，DB 零污染；
- 保留策略与重跑复用（替代已删除的 fetch_progress）
  - incremental：文件保留到其交易日被 sync_state 水位覆盖后才允许清理；
  - range：applied 成功后即可清理；
  - 兜底：retention_days 清理孤儿/过期文件。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from yafip.core.db import Database, now_iso
from yafip.data.repository import Repository

DATASET_BARS = "bars"
DATASET_META = "meta"

KIND_BARS = "bars"
KIND_ADJ = "adj"
KIND_INSTRUMENT = "instrument"
KIND_METRIC = "metric"

CREATED = "created"
VALIDATED = "validated"
APPLIED = "applied"
FAILED = "failed"
ARCHIVED = "archived"

BARS_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "pre_close", "vol", "amount",
]
ADJ_COLUMNS = ["symbol", "trade_date", "adj_factor"]
INSTRUMENT_COLUMNS = [
    "symbol", "name", "asset_type", "exchange", "currency",
    "list_date", "delist_date", "status", "ext_json",
]
METRIC_COLUMNS = ["symbol", "metric_key", "metric_value", "unit", "as_of_date"]

# DB 提交单写队列：staging 抓取/校验并行，提交串行，
# 避免 SQLite 写锁竞争（database is locked）。
_COMMIT_LOCK = threading.Lock()


class StagingError(ValueError):
    code = "7001"
    http_status = 400


class StagingStore:
    def __init__(self, root: Path, db: Database, repo: Repository, audit: Any = None):
        self.root = Path(root) / "data" / "staging"
        self.db = db
        self.repo = repo
        self.audit = audit  # AuditService；None 时跳过审计（测试友好）

    # ------------------------------------------------------------------
    # 路径与文件
    # ------------------------------------------------------------------
    def _batch_dir(self, dataset: str, market: str, batch_id: str) -> Path:
        return self.root / dataset / market / batch_id

    def _manifest_path(self, dataset: str, market: str, batch_id: str) -> Path:
        return self._batch_dir(dataset, market, batch_id) / "manifest.json"

    @staticmethod
    def _checksum(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # 写单元
    # ------------------------------------------------------------------
    def write_unit(
        self,
        *,
        batch_id: str,
        task_id: str,
        task_type: str,
        executor: str,
        source: str,
        market: str,
        dataset: str,
        unit_key: str,
        kind: str,
        df: pd.DataFrame,
        params_snapshot: dict[str, Any],
        columns: list[str] | None = None,
    ) -> Path:
        """写单元 CSV + 登记 manifest；返回 CSV 路径。"""
        batch_dir = self._batch_dir(dataset, market, batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        cols = columns or list(df.columns)
        csv_path = batch_dir / f"unit_{unit_key}_{kind}.csv"
        df[cols].to_csv(csv_path, index=False)
        checksum = self._checksum(csv_path.read_text(encoding="utf-8"))

        manifest = self._load_manifest(dataset, market, batch_id)
        if manifest is None:
            manifest = {
                "batch_id": batch_id,
                "task_id": task_id,
                "task_type": task_type,
                "executor": executor,
                "source": source,
                "market": market,
                "dataset": dataset,
                "params_snapshot": params_snapshot,
                "created_at": now_iso(),
                "units": {},
            }
        # 单元标的集合：复用过滤用（与当前任务的目标集合做全等比较，防止串任务）
        symbols = sorted({str(s) for s in df["symbol"].dropna().unique()}) if "symbol" in df.columns else []
        manifest["units"][csv_path.name] = {
            "unit_key": unit_key,
            "kind": kind,
            "rows": int(len(df)),
            "columns": cols,
            "checksum": checksum,
            "symbols": symbols,
            "status": CREATED,
            "error": None,
        }
        self._save_manifest(dataset, market, batch_id, manifest)
        self._sync_batch_table(batch_id, manifest)
        return csv_path

    def _load_manifest(self, dataset: str, market: str, batch_id: str) -> dict | None:
        path = self._manifest_path(dataset, market, batch_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_manifest(self, dataset: str, market: str, batch_id: str, manifest: dict) -> None:
        path = self._manifest_path(dataset, market, batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sync_batch_table(self, batch_id: str, manifest: dict) -> None:
        total = sum(u["rows"] for u in manifest["units"].values())
        statuses = {u["status"] for u in manifest["units"].values()}
        status = APPLIED if statuses == {APPLIED} else FAILED if FAILED in statuses else (
            VALIDATED if statuses == {VALIDATED} else CREATED
        )
        self.db.execute(
            """
            INSERT INTO staging_batch(
                batch_id, task_id, market, dataset, file_path, rows, checksum,
                status, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(batch_id) DO UPDATE SET
                task_id=excluded.task_id, market=excluded.market, dataset=excluded.dataset,
                file_path=excluded.file_path, rows=excluded.rows, checksum=excluded.checksum,
                status=excluded.status
            """,
            (
                batch_id, manifest.get("task_id"), manifest.get("market"),
                manifest.get("dataset"), str(self._manifest_path(
                    manifest["dataset"], manifest["market"], batch_id)),
                total, "", status, now_iso(),
            ),
        )

    def mark_unit_status(
        self,
        dataset: str,
        market: str,
        batch_id: str,
        file_name: str,
        status: str,
        *,
        error: str | None = None,
        validated_at: str | None = None,
        applied_at: str | None = None,
    ) -> None:
        manifest = self._load_manifest(dataset, market, batch_id)
        if manifest is None or file_name not in manifest["units"]:
            raise StagingError(f"staging 单元不存在：{batch_id}/{file_name}")
        unit = manifest["units"][file_name]
        unit["status"] = status
        if error is not None:
            unit["error"] = error
        if validated_at:
            unit["validated_at"] = validated_at
        if applied_at:
            unit["applied_at"] = applied_at
        self._save_manifest(dataset, market, batch_id, manifest)
        self._sync_batch_table(batch_id, manifest)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate_unit(
        self,
        unit_path: Path,
        *,
        market: str,
        kind: str,
        start: str | None = None,
        end: str | None = None,
        whitelist_codes: set[str] | None = None,
        enforce_whitelist: bool = False,
        adj_policy: tuple[str, float | None] = ("FACTOR", None),
    ) -> list[str]:
        """返回校验错误列表；空列表 = 通过。"""
        errors: list[str] = []
        try:
            df = pd.read_csv(unit_path)
        except Exception as exc:  # noqa: BLE001
            return [f"CSV 读取失败：{exc}"]

        if df.empty:
            return ["行数为 0"]

        if kind == KIND_BARS:
            errors += self._validate_bars(df, market, start, end, whitelist_codes, enforce_whitelist)
        elif kind == KIND_ADJ:
            adj_type, fallback = adj_policy
            if adj_type == "FACTOR" and (df.get("adj_factor") is None or df["adj_factor"].isna().any()):
                errors.append("复权因子缺失（FACTOR 模式不允许空值）")
            errors += self._validate_key_unique(df, market)
        elif kind == KIND_INSTRUMENT:
            if "symbol" not in df.columns or df["symbol"].isna().any():
                errors.append("instrument 单元缺少 symbol")
        elif kind == KIND_METRIC:
            if "symbol" not in df.columns or "metric_key" not in df.columns:
                errors.append("metric 单元缺少 symbol/metric_key")
        else:
            errors.append(f"未知单元类型：{kind}")
        return errors

    def _validate_key_unique(self, df: pd.DataFrame, market: str) -> list[str]:
        errors: list[str] = []
        if df.duplicated(subset=["symbol", "trade_date"]).any():
            errors.append("(symbol, trade_date) 存在重复行")
        return errors

    def _validate_bars(
        self,
        df: pd.DataFrame,
        market: str,
        start: str | None,
        end: str | None,
        whitelist_codes: set[str] | None,
        enforce_whitelist: bool,
    ) -> list[str]:
        errors: list[str] = []
        required = {"symbol", "trade_date", "open", "high", "low", "close", "vol", "amount"}
        missing_cols = required - set(df.columns)
        if missing_cols:
            return [f"缺少必填列：{', '.join(sorted(missing_cols))}"]

        # amount 可空（美股无 amount 接口）；OHLC + vol 必须非空
        nonnull = df[["open", "high", "low", "close", "vol"]].notna()
        if not nonnull.all().all():
            errors.append("必填列存在空值")
        for column in ("open", "high", "low", "close"):
            if (df[column] < 0).any():
                errors.append(f"价格列 {column} 出现负数")
        bad_ohlc = df[(df["high"] < df[["open", "close", "low"]].max(axis=1)) |
                      (df["low"] > df[["open", "close", "high"]].min(axis=1))]
        if not bad_ohlc.empty:
            errors.append("OHLC 逻辑不成立（high 非最高或 low 非最低）")

        dates = [str(d) for d in df["trade_date"]]
        bad_dates = [d for d in dates if len(d) != 10]
        if bad_dates:
            errors.append(f"存在非法日期：{bad_dates[:3]}")
        if start and end:
            out_of_range = [d for d in dates if d < start or d > end]
            if out_of_range:
                errors.append(f"日期超出请求区间：{out_of_range[:3]}")

        errors += self._validate_key_unique(df, market)

        if enforce_whitelist and whitelist_codes is not None:
            outside = set(df["symbol"]) - whitelist_codes
            if outside:
                errors.append(f"含非白名单标的：{sorted(outside)[:5]}")
        return errors

    # ------------------------------------------------------------------
    # 提交（单事务，原子）
    # ------------------------------------------------------------------
    def commit_units(
        self,
        dataset: str,
        market: str,
        units: list[tuple[Path, str]],  # (csv_path, kind)；batch 由路径推导（支持跨 batch 复用）
        *,
        source: str,
        adj_policy: tuple[str, float | None] = ("FACTOR", None),
        currency: str | None = None,
        vol_unit: str | None = None,
        amount_unit: str | None = None,
    ) -> int:
        """单事务批量入库 + 状态登记（各自 manifest applied）；任一失败整体回滚。

        batch 由每个单元路径的父目录推导：同一任务新写的单元与重跑复用的
        （被 kill 的旧 batch）validated 单元可在一次提交中同时入库。
        """
        with _COMMIT_LOCK:  # 单写队列：全库提交串行
            return self._commit_locked(
                dataset, market, units,
                source=source, adj_policy=adj_policy,
                currency=currency, vol_unit=vol_unit, amount_unit=amount_unit,
            )

    def _commit_locked(
        self,
        dataset: str,
        market: str,
        units: list[tuple[Path, str]],
        *,
        source: str,
        adj_policy: tuple[str, float | None],
        currency: str | None,
        vol_unit: str | None,
        amount_unit: str | None,
    ) -> int:
        batches: set[str] = set()
        try:
            with self.db.tx():
                for csv_path, kind in units:
                    batch_id = csv_path.parent.name
                    batches.add(batch_id)
                    manifest = self._load_manifest(dataset, market, batch_id)
                    if manifest is None:
                        raise StagingError(f"batch 不存在：{batch_id}（{csv_path.name}）")
                    df = pd.read_csv(csv_path)
                    if kind == KIND_BARS:
                        self.repo.upsert_bars(
                            market, df, source=source,
                            currency=currency, vol_unit=vol_unit, amount_unit=amount_unit,
                        )
                    elif kind == KIND_ADJ:
                        adj_type, _fallback = adj_policy
                        self.repo.upsert_adj(market, df, source=source, adj_type=adj_type)
                    elif kind == KIND_INSTRUMENT:
                        self.repo.upsert_instrument(market, df.to_dict("records"), source=source)
                    elif kind == KIND_METRIC:
                        for record in df.to_dict("records"):
                            self.repo.upsert_instrument_metric(
                                market, record["symbol"], record["metric_key"],
                                record.get("metric_value"),
                                unit=record.get("unit"), as_of_date=record.get("as_of_date"),
                                source=source,
                            )
                    self.mark_unit_status(
                        dataset, market, batch_id, csv_path.name, APPLIED,
                        applied_at=now_iso(),
                    )
        except Exception as exc:  # noqa: BLE001
            # 回滚由 tx() 负责；标记失败单元，文件保留供人工核对与重跑
            for csv_path, _kind in units:
                try:
                    self.mark_unit_status(
                        dataset, market, csv_path.parent.name, csv_path.name, FAILED,
                        error=str(exc)[:500],
                    )
                except StagingError:
                    pass
            raise StagingError(f"staging 提交失败（已回滚）：{exc}") from exc
        if self.audit is not None:
            self.audit.write(
                "staging.commit", ",".join(sorted(batches)),
                f"units={len(units)} market={market}",
            )
        return len(units)

    # ------------------------------------------------------------------
    # 重跑复用与保留策略
    # ------------------------------------------------------------------
    def reusable_units(
        self,
        dataset: str,
        market: str,
        *,
        kinds: tuple[str, ...] | None = None,
        task_type: str | None = None,
        executor: str | None = None,
        symbols: set[str] | None = None,
    ) -> list[dict]:
        """扫描已有 batch：返回 status in (validated, applied) 且未清理的单元。

        全维度过滤：任务类型 / 执行器 / 标的集合必须与当前任务一致，
        防止 bars.range 遗留文件被 bars.incremental 误用。
        """
        found: list[dict] = []
        base = self.root / dataset / market
        if not base.exists():
            return found
        for batch_dir in sorted(base.iterdir()):
            if not batch_dir.is_dir():
                continue
            manifest = self._load_manifest(dataset, market, batch_dir.name)
            if manifest is None:
                continue
            if task_type is not None and manifest.get("task_type") != task_type:
                continue
            if executor is not None and manifest.get("executor") != executor:
                continue
            for file_name, unit in manifest.get("units", {}).items():
                if unit["status"] not in (VALIDATED, APPLIED):
                    continue
                if kinds and unit["kind"] not in kinds:
                    continue
                if symbols is not None and set(unit.get("symbols") or []) != symbols:
                    continue
                path = batch_dir / file_name
                if not path.exists():
                    continue
                found.append({
                    "batch_id": batch_dir.name,
                    "file_name": file_name,
                    "path": path,
                    "kind": unit["kind"],
                    "unit_key": unit["unit_key"],
                    "status": unit["status"],
                    "columns": unit["columns"],
                    "task_type": manifest.get("task_type"),
                    "executor": manifest.get("executor"),
                    "symbols": unit.get("symbols"),
                })
        return found

    def cleanup(
        self,
        retention_days: int,
        watermark_by_market: dict[str, str | None],
    ) -> int:
        """按保留策略清理
        - incremental 文件保留到其交易日 <= 水位（水位覆盖后才可清理）；
        - range 文件 applied 后即可清理；
        - 兜底：created/failed 超过 retention_days 的孤儿文件清理。
        只删已 applied 或过期的文件；清理写 audit 由调用方负责。
        """
        removed = 0
        cutoff = (date.today() - timedelta(days=max(1, retention_days))).isoformat()
        base = self.root
        if not base.exists():
            return 0
        for dataset_dir in base.iterdir():
            if not dataset_dir.is_dir():
                continue
            for market_dir in dataset_dir.iterdir():
                if not market_dir.is_dir():
                    continue
                market = market_dir.name
                watermark = watermark_by_market.get(market)
                for batch_dir in market_dir.iterdir():
                    if not batch_dir.is_dir():
                        continue
                    manifest = self._load_manifest(dataset_dir.name, market, batch_dir.name)
                    if manifest is None:
                        shutil.rmtree(batch_dir, ignore_errors=True)
                        removed += 1
                        continue
                    created = str(manifest.get("created_at") or "")[:10]
                    task_type = manifest.get("task_type")
                    removable = True
                    for file_name, unit in manifest.get("units", {}).items():
                        status = unit["status"]
                        if status in (CREATED, FAILED):
                            if created >= cutoff:
                                removable = False
                                break
                        elif status == VALIDATED:
                            removable = False  # 已校验未提交：保留用于复用
                            break
                        elif status == APPLIED:
                            # incremental 文件保留到水位覆盖其交易日；
                            # 无水位（从未成功推进）也必须保留——水位保护不是"有水位才保护"
                            if (
                                dataset_dir.name == DATASET_BARS
                                and task_type == "bars.incremental"
                                and (watermark is None or unit["unit_key"] > watermark)
                            ):
                                removable = False
                                break
                            # range applied / meta applied / incremental 已被水位覆盖 → 可清理
                    if removable:
                        shutil.rmtree(batch_dir, ignore_errors=True)
                        removed += 1
        if removed and self.audit is not None:
            self.audit.write("staging.cleanup", str(self.root), f"removed={removed}")
        return removed
