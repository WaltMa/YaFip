"""运行时配置：全部存 app_config，读多写少，内存缓存 + 写后热加载。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from yafip.core.db import Database, now_iso

# 配置键语义：fetch.rpm = 每分钟请求数（旧键 fetch.qps / batch_size / mv_* 已废弃）
DEFAULTS: dict[str, str] = {
    "server.port": "8203",
    "server.stop_wait_sec": "0",
    "task.max_workers": "2",
    "task.max_concurrency": "10",
    "task.cpu_ratio": "0.10",
    "task.cpu_budget_ratio": "0.50",
    "fetch.rpm": "120",
    "fetch.meta_rpm": "1",
    "fetch.timeout_sec": "15",
    "fetch.retry_max": "3",
    "data.start_date": "2010-01-01",
    "data.include_delisted": "1",
    "whitelist.metric": "total_mv",
    "whitelist.threshold_wan": "500000.0",
    "staging.retention_days": "30",
    "task.retention_days": "90",
    "sse.heartbeat_sec": "15",
}

# CPU 并发限流键已落地（DEFAULTS + VALIDATORS），
# 界面「系统设置」可见可配；优先级「真实环境变量 > .env > 界面配置」。

# 2026-11 起默认端口由 8765 调整为 8203，启动时自动迁移旧默认值（一次性）
LEGACY_SERVER_PORT = "8765"
PORT_MIGRATION_KEY = "_migration.server_port_8765"

# 历史遗留 key，读取时忽略、写入时拒绝，避免双份真相
REMOVED_KEYS = {
    "fetch.qps",
    "fetch.batch_size",
    "data.mv_threshold_wan",
    "data.mv_metric",
    "data.mv_period",
    "data.whitelist_rule_id",
}

# 数据源配置（token 等）并入 app_config，但只经「数据源配置」页读写。
# 从公开视图（/api/config）过滤，并在 set_many 拒绝，防止 token 明文外泄
# （datasource_config 时代注释：token 不写日志、不出现在 API 响应）。
DATASOURCE_KEY_PREFIX = "datasource."


class ConfigError(ValueError):
    pass


def _require_int(key: str, value: Any, lo: int, hi: int | None) -> None:
    try:
        num = int(float(str(value)))
    except (TypeError, ValueError):
        raise ConfigError(f"{key} 必须是整数，收到：{value}") from None
    if num < lo or (hi is not None and num > hi):
        limit = "以上" if hi is None else f"~{hi}"
        raise ConfigError(f"{key} 取值范围应为 {lo}{limit}，收到：{num}")


def _require_float(key: str, value: Any, lo: float, hi: float | None) -> None:
    try:
        num = float(str(value))
    except (TypeError, ValueError):
        raise ConfigError(f"{key} 必须是数字，收到：{value}") from None
    if num < lo or (hi is not None and num > hi):
        limit = "以上" if hi is None else f"~{hi}"
        raise ConfigError(f"{key} 取值范围应为 {lo}{limit}，收到：{num}")


def _require_bool(key: str, value: Any) -> None:
    if str(value).strip().lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise ConfigError(f"{key} 必须是布尔值（0/1），收到：{value}")


def _require_iso_date(key: str, value: Any) -> None:
    text = str(value).strip()
    try:
        datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        raise ConfigError(f"{key} 必须是 YYYY-MM-DD 日期，收到：{value}") from None


# 写入校验：防止 0 值触发 SSE 死循环（heartbeat_sec=0 → wait_for 立即超时）、
# 非法端口/负数并发等让服务进入不可用状态。未列出的 key 不做值校验。
VALIDATORS: dict[str, Callable[[Any], None]] = {
    "server.port": lambda v: _require_int("server.port", v, 1, 65535),
    "server.stop_wait_sec": lambda v: _require_int("server.stop_wait_sec", v, 0, 86400),
    "task.max_workers": lambda v: _require_int("task.max_workers", v, 1, 64),
    "task.max_concurrency": lambda v: _require_int("task.max_concurrency", v, 1, 64),
    "task.cpu_ratio": lambda v: _require_float("task.cpu_ratio", v, 0.01, 1.0),
    "task.cpu_budget_ratio": lambda v: _require_float("task.cpu_budget_ratio", v, 0.01, 1.0),
    "fetch.rpm": lambda v: _require_int("fetch.rpm", v, 1, 100000),
    "fetch.meta_rpm": lambda v: _require_int("fetch.meta_rpm", v, 1, 100000),
    "fetch.timeout_sec": lambda v: _require_int("fetch.timeout_sec", v, 1, 600),
    "fetch.retry_max": lambda v: _require_int("fetch.retry_max", v, 0, 20),
    "task.retention_days": lambda v: _require_int("task.retention_days", v, 1, 3650),
    "staging.retention_days": lambda v: _require_int("staging.retention_days", v, 1, 365),
    "sse.heartbeat_sec": lambda v: _require_int("sse.heartbeat_sec", v, 1, 3600),
    "data.start_date": lambda v: _require_iso_date("data.start_date", v),
    "data.include_delisted": lambda v: _require_bool("data.include_delisted", v),
    "whitelist.enabled": lambda v: _require_bool("whitelist.enabled", v),
    "whitelist.threshold_wan": lambda v: _require_float("whitelist.threshold_wan", v, 0.0, None),
}


class ConfigService:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}
        self.reload()

    def seed_defaults(self) -> None:
        now = now_iso()
        with self.db.tx() as conn:
            for key, value in DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO app_config(key, value, updated_at) VALUES(?,?,?)",
                    (key, value, now),
                )
            # 旧默认端口迁移：仅在用户从未改过（仍为 8765）时生效，且只执行一次
            migrated = conn.execute(
                "SELECT 1 FROM app_config WHERE key=?", (PORT_MIGRATION_KEY,)
            ).fetchone()
            if migrated is None:
                conn.execute(
                    "UPDATE app_config SET value=?, updated_at=? WHERE key='server.port' AND value=?",
                    (DEFAULTS["server.port"], now, LEGACY_SERVER_PORT),
                )
                conn.execute(
                    "INSERT INTO app_config(key, value, updated_at) VALUES(?,?,?)",
                    (PORT_MIGRATION_KEY, "1", now),
                )
        self.reload()

    def reload(self) -> None:
        rows = self.db.query("SELECT key, value FROM app_config")
        with self._lock:
            self._cache = {row["key"]: row["value"] for row in rows}

    # -- 读取 ---------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        return DEFAULTS.get(key, default)

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return default if value is None else str(value)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(str(self.get(key, str(default)))))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(str(self.get(key, str(default))))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, None)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def all(self) -> dict[str, str]:
        merged = dict(DEFAULTS)
        with self._lock:
            merged.update(
                {
                    k: v
                    for k, v in self._cache.items()
                    if not k.startswith("_") and not k.startswith(DATASOURCE_KEY_PREFIX)
                }
            )
        return merged

    # -- 写入 ---------------------------------------------------------------
    def set_many(self, values: dict[str, Any]) -> dict[str, str]:
        unknown = [k for k in values if k not in DEFAULTS]
        removed = [k for k in values if k in REMOVED_KEYS]
        datasource_keys = [k for k in values if k.startswith(DATASOURCE_KEY_PREFIX)]
        if datasource_keys:
            raise ConfigError(
                f"数据源配置请在「数据源配置」页维护（避免经 /api/config 泄露 token）：{', '.join(datasource_keys)}"
            )
        if removed:
            raise ConfigError(f"配置项已废弃：{', '.join(removed)}")
        if unknown:
            raise ConfigError(f"未知配置项：{', '.join(unknown)}")

        # 先整体校验再落库，避免事务中途失败留下部分写入
        now = now_iso()
        text_values: list[tuple[str, str]] = []
        for key, value in values.items():
            text = "1" if value is True else "0" if value is False else str(value)
            validator = VALIDATORS.get(key)
            if validator is not None:
                validator(text)
            text_values.append((key, text))

        with self.db.tx() as conn:
            for key, text in text_values:
                conn.execute(
                    """
                    INSERT INTO app_config(key, value, updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, text, now),
                )
        self.reload()
        return self.all()
