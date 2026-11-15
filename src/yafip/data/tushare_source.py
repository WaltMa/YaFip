"""tushare pro 适配器（本期主力数据源）。

- 令牌桶限速（fetch.rpm 次/分钟）
- 指数退避重试（fetch.retry_max）
- token / 积分 / 权限类错误立即 Fatal，不静默降级
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

from yafip.config import ConfigService
from yafip.data.base import (
    ADJ_COLUMNS,
    BASIC_COLUMNS,
    CALENDAR_COLUMNS,
    DAILY_COLUMNS,
    MV_COLUMNS,
    DataSource,
    DataSourceError,
    FatalDataSourceError,
    TokenInvalid,
    ensure_columns,
    normalize_date,
    to_tushare_date,
)
from yafip.data.repository import Repository

FATAL_HINTS = (
    "token",
    "权限",
    "积分",
    "没有访问",
    "无权",
    "不存在",
    "invalid",
    "permission",
    "not found",
)

RATE_LIMIT_RE = re.compile(r"频率超限\((\d+)次/(?:(\d+))?(秒|分钟|小时)\)")
META_INTERFACES = {"trade_cal", "stock_basic"}
RATE_LIMIT_WAIT_MAX = 180.0


class RateLimiter:
    """滑动窗口限速：每个窗口最多 n 次。"""

    def __init__(self, limit: int, window_sec: float = 60.0):
        self.limit = max(1, int(limit))
        self.window_sec = float(window_sec)
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def set_rate(self, limit: int, window_sec: float = 60.0) -> None:
        with self._lock:
            self.limit = max(1, int(limit))
            self.window_sec = float(window_sec)

    def wait_seconds(self) -> float:
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= self.window_sec:
                self._times.popleft()
            if len(self._times) < self.limit:
                return 0.0
            return max(0.0, self.window_sec - (now - self._times[0]))

    def try_acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= self.window_sec:
                self._times.popleft()
            if len(self._times) >= self.limit:
                return False
            self._times.append(now)
            return True

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window_sec:
                    self._times.popleft()
                if len(self._times) < self.limit:
                    self._times.append(now)
                    return
                wait = self.window_sec - (now - self._times[0]) + 0.01
            time.sleep(min(max(wait, 0.05), 5.0))


class TushareSource(DataSource):
    name = "tushare"

    def __init__(self, repo: Repository, config: ConfigService):
        self.repo = repo
        self.config = config
        self._client: Any = None
        self._token: str | None = None
        self._api_url: str | None = None
        self._limiters: dict[str, RateLimiter] = {}
        # 服务端限频错误解析出的真实限额：本进程内保持该值，
        # 避免 _limiter_for 每次循环顶部又用配置值覆盖回去
        self._server_rates: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    # -- 客户端 -------------------------------------------------------------
    def _pro(self, *, check_enabled: bool = True):
        row = self.repo.get_datasource("tushare") or {}
        if check_enabled and not bool(row.get("enabled", 1)):
            raise DataSourceError("tushare 数据源已停用，请在「数据源配置」页启用")
        token = str(row.get("token") or "").strip()
        api_url = str(row.get("api_url") or "").strip()
        if not token:
            raise TokenInvalid("未配置 tushare token，请在「数据源配置」页录入")
        with self._lock:
            if self._client is None or token != self._token or api_url != self._api_url:
                import tushare as ts

                timeout = self.config.get_int("fetch.timeout_sec", 15)
                try:
                    client = ts.pro_api(token, timeout=timeout)
                except TypeError:
                    client = ts.pro_api(token)
                if api_url:
                    # tushare 官方客户端未暴露 server 参数，兼容自定义网关时设置私有属性
                    try:
                        client._DataApi__http_url = api_url.rstrip("/")
                    except Exception:  # noqa: BLE001 - 自定义网关失败时回退官方地址
                        pass
                self._client = client
                self._token = token
                self._api_url = api_url
            return self._client

    def _limiter_for(self, interface: str) -> RateLimiter:
        with self._lock:
            negotiated = self._server_rates.get(interface)
            if negotiated is not None:
                # 服务端限频错误已协商出真实限额（可能远低于配置值），
                # 保持该值直至重启，不要被配置覆盖回去
                limiter = self._limiters.get(interface)
                if limiter is None:
                    limiter = RateLimiter(*negotiated)
                    self._limiters[interface] = limiter
                return limiter
            if interface in META_INTERFACES:
                limit = self.config.get_int("fetch.meta_rpm", 1)
            else:
                limit = self.repo.datasource_rpm() or self.config.get_int("fetch.rpm", 120)
            limiter = self._limiters.get(interface)
            if limiter is None:
                limiter = RateLimiter(limit)
                self._limiters[interface] = limiter
            else:
                limiter.set_rate(limit)
            return limiter

    @staticmethod
    def _parse_rate_limit(message: str) -> tuple[int, int] | None:
        match = RATE_LIMIT_RE.search(message)
        if not match:
            return None
        per = max(1, int(match.group(1)))
        value = max(1, int(match.group(2) or 1))
        unit = match.group(3)
        seconds = value * {"秒": 1, "分钟": 60, "小时": 3600}[unit]
        return per, seconds

    def _call(
        self,
        func: Callable[..., Any],
        interface: str,
        *,
        wait_on_limit: bool = True,
        **kwargs: Any,
    ):
        retry_max = max(0, self.config.get_int("fetch.retry_max", 3))
        last_error: Exception | None = None
        attempt = 0
        limit_retries = 0

        while attempt <= retry_max:
            limiter = self._limiter_for(interface)
            if wait_on_limit:
                wait = limiter.wait_seconds()
                if wait > RATE_LIMIT_WAIT_MAX:
                    raise DataSourceError(
                        f"tushare 接口 {interface} 本地限频窗口需等待 {wait:.0f} 秒，"
                        f"超过自动等待上限；请稍后重试或提升积分"
                    )
                limiter.acquire()
            else:
                if not limiter.try_acquire():
                    wait = limiter.wait_seconds()
                    raise DataSourceError(
                        f"tushare 接口 {interface} 本地限频窗口未释放（还需约 {wait:.0f} 秒），请稍后重试"
                    )
            try:
                result = func(**kwargs)
                if result is None:
                    raise DataSourceError("数据源返回空结果（非数据集为空）")
                return result
            except FatalDataSourceError:
                raise
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                rate = self._parse_rate_limit(message)
                if rate is not None:
                    per, window_sec = rate
                    with self._lock:
                        self._server_rates[interface] = (per, window_sec)
                    self._limiter_for(interface).set_rate(per, window_sec)
                    wait = window_sec / per + 1.0
                    if not wait_on_limit or wait > RATE_LIMIT_WAIT_MAX or limit_retries >= 3:
                        raise DataSourceError(
                            f"tushare 接口 {interface} 频率超限（{per} 次/{window_sec} 秒），"
                            f"约需等待 {wait:.0f} 秒；请降低 fetch.rpm / fetch.meta_rpm 或稍后重试"
                        ) from exc
                    limit_retries += 1
                    time.sleep(wait)
                    continue
                if any(hint in message.lower() for hint in FATAL_HINTS):
                    raise FatalDataSourceError(f"tushare 接口不可用：{message}") from exc
                last_error = exc
                attempt += 1
                if attempt > retry_max:
                    break
                time.sleep(min(2 ** (attempt - 1), 10))
        raise DataSourceError(f"tushare 请求失败（已重试 {retry_max} 次）：{last_error}")

    # -- 接口 ---------------------------------------------------------------
    def _require_cn_a(self, market: str) -> None:
        if market != "CN_A":
            raise DataSourceError(f"tushare 仅支持 A 股（CN_A），收到市场：{market}（港美股请用 akshare 执行器）")

    def get_stock_list(self, market: str, include_delisted: bool = True) -> pd.DataFrame:
        self._require_cn_a(market)
        pro = self._pro()
        statuses = ["L", "D", "P"] if include_delisted else ["L"]
        frames: list[pd.DataFrame] = []
        for status in statuses:
            df = self._call(
                pro.stock_basic,
                "stock_basic",
                exchange="",
                list_status=status,
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date",
            )
            if df is None or df.empty:
                continue
            df = df.copy()
            df["list_status"] = status
            df["is_north"] = 0
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=BASIC_COLUMNS)
        result = pd.concat(frames, ignore_index=True)
        result["list_date"] = result["list_date"].map(normalize_date)
        result["delist_date"] = result["delist_date"].map(normalize_date)
        return ensure_columns(result, BASIC_COLUMNS)

    def get_calendar(self, market: str, start: str, end: str) -> pd.DataFrame:
        self._require_cn_a(market)
        pro = self._pro()
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        frames: list[pd.DataFrame] = []
        cursor = start_date
        while cursor <= end_date:
            # 单次最多返回 6000 行，按 10 年分段
            chunk_end = min(date(cursor.year + 9, 12, 31), end_date)
            df = self._call(
                pro.trade_cal,
                "trade_cal",
                exchange="SSE",
                start_date=to_tushare_date(cursor.isoformat()),
                end_date=to_tushare_date(chunk_end.isoformat()),
            )
            if df is not None and not df.empty:
                frames.append(df)
            cursor = chunk_end + timedelta(days=1)

        if not frames:
            return pd.DataFrame(columns=CALENDAR_COLUMNS)
        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["cal_date"]).copy()
        result["cal_date"] = result["cal_date"].map(normalize_date)
        result["pretrade_date"] = result["pretrade_date"].map(normalize_date)
        result["is_open"] = result["is_open"].astype(int)
        result["exchange"] = "SSE"
        result = result.sort_values("cal_date").reset_index(drop=True)
        return ensure_columns(result, CALENDAR_COLUMNS)

    def get_daily(
        self,
        market: str,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        self._require_cn_a(market)
        pro = self._pro()
        df = self._call(pro.daily, "daily", trade_date=to_tushare_date(trade_date))
        if df is None or df.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        df = df.copy()
        df["trade_date"] = df["trade_date"].map(normalize_date)
        return ensure_columns(df, DAILY_COLUMNS)

    def get_adj_factor(self, market: str, trade_date: str) -> pd.DataFrame:
        self._require_cn_a(market)
        pro = self._pro()
        df = self._call(pro.adj_factor, "adj_factor", trade_date=to_tushare_date(trade_date))
        if df is None or df.empty:
            return pd.DataFrame(columns=ADJ_COLUMNS)
        df = df.copy()
        df["trade_date"] = df["trade_date"].map(normalize_date)
        return ensure_columns(df, ADJ_COLUMNS)

    def get_daily_basic(self, market: str, trade_date: str) -> pd.DataFrame:
        self._require_cn_a(market)
        pro = self._pro()
        df = self._call(
            pro.daily_basic,
            "daily_basic",
            trade_date=to_tushare_date(trade_date),
            fields="ts_code,trade_date,total_mv",
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=MV_COLUMNS)
        df = df.copy()
        df["trade_date"] = df["trade_date"].map(normalize_date)
        return ensure_columns(df, MV_COLUMNS)

    # -- 连接测试 -----------------------------------------------------------
    def test_connection(self) -> dict:
        steps: list[dict] = []
        row = self.repo.get_datasource("tushare") or {}
        token = str(row.get("token") or "").strip()
        if not token:
            return {"ok": False, "steps": [{"name": "token", "ok": False, "message": "未配置 token"}]}

        try:
            pro = self._pro(check_enabled=False)
            start = (date.today() - timedelta(days=30)).isoformat()
            end = date.today().isoformat()
            cal = self._call(
                pro.trade_cal,
                "trade_cal",
                wait_on_limit=False,
                exchange="SSE",
                start_date=to_tushare_date(start),
                end_date=to_tushare_date(end),
            )
            opens = cal[cal["is_open"] == 1]["cal_date"].tolist() if cal is not None else []
            last_open = max(normalize_date(item) for item in opens) if opens else (
                date.today() - timedelta(days=1)
            ).isoformat()
            steps.append({"name": "trade_cal", "ok": True, "message": f"最近交易日 {last_open}"})
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "steps": steps + [{"name": "trade_cal", "ok": False, "message": str(exc)}],
            }

        for name, func in (("daily", pro.daily), ("daily_basic", pro.daily_basic)):
            try:
                df = self._call(
                    func,
                    name,
                    wait_on_limit=False,
                    trade_date=to_tushare_date(last_open),
                )
                count = 0 if df is None else len(df)
                steps.append(
                    {
                        "name": name,
                        "ok": count > 0,
                        "message": f"{last_open} 返回 {count} 行" if count else "返回 0 行（积分或权限不足）",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                steps.append({"name": name, "ok": False, "message": str(exc)})

        ok = all(step["ok"] for step in steps)
        self.repo.set_datasource_test("tushare", ok, "; ".join(s["message"] for s in steps if not s["ok"]) or "ok")
        return {"ok": ok, "steps": steps}
