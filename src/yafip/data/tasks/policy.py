"""复权策略与市场单位钩子（/）。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from yafip.data.market import MARKET_CN_A, MARKET_HK, MARKET_US


def resolve_adj_policy(market: str) -> tuple[str, float | None]:
    """返回 (adj_type, 因子兜底值)。

    - A 股：`FACTOR`，因子来自数据源（缺失即单元失败，不静默降级）；
    - HK/US：`NONE`，因子恒 1.0；具体 split/dividend 折算待数据源确定后实现。
    """
    if market == MARKET_CN_A:
        return "FACTOR", None
    return "NONE", 1.0


def market_units(market: str) -> tuple[str, str, str]:
    """市场单位约定：(currency, vol_unit, amount_unit)，executor 边界显式落库。"""
    if market == MARKET_HK:
        return "HKD", "股", "港元"
    if market == MARKET_US:
        return "USD", "股", "美元"
    return "CNY", "手", "千元"


# 各市场收盘时刻（市场本地时间）：CN_A 15:00（北京时间）、HK 16:00（香港时间）、US 16:00（美东时间）
_MARKET_CLOSE_TIME = {
    MARKET_CN_A: time(15, 0),
    MARKET_HK: time(16, 0),
    MARKET_US: time(16, 0),
}
# US 近似 DST：4~10 月 EDT(UTC-4)，其余 EST(UTC-5)。
# 3/11 月过渡期 ±2 周误差只影响收盘前后 1 小时内的判定，方向偏保守
# （至多多等一天才抓今天，绝不把盘中快照当作已收盘）。
_US_EDT_MONTHS = {4, 5, 6, 7, 8, 9, 10}


def _market_utc_offset(market: str, now_utc: datetime) -> timedelta:
    """市场本地时刻 ≈ UTC 时刻的偏移（零依赖；US 用近似 DST）。"""
    if market in (MARKET_CN_A, MARKET_HK):
        return timedelta(hours=8)
    if market == MARKET_US:
        return timedelta(hours=-4) if now_utc.month in _US_EDT_MONTHS else timedelta(hours=-5)
    return timedelta(0)


def market_local_datetime(market: str, now_utc: datetime | None = None) -> datetime:
    """把 UTC 时刻转换为市场本地日期+时间（增量“今天”的判定基准）。

    跨日安全：UTC 1/5 22:00 对 CN_A → 本地 1/6 06:00（日期也随偏移进位/退位），
    不再出现「UTC 小时 ≥ 阈值但本地还是盘前」的误判。
    """
    now = now_utc or datetime.utcnow()
    return now + _market_utc_offset(market, now)


def market_local_date(market: str, now_utc: datetime | None = None) -> str:
    """市场本地“今天”的 ISO 日期。"""
    return market_local_datetime(market, now_utc).date().isoformat()


def market_closed(market: str, now_utc: datetime | None = None) -> bool:
    """市场本地日期+时间是否已过当日收盘时刻。

    增量据此决定是否纳入“今天”——必须是市场日历里的今天，
    不是服务器本地日期（US 交易日相对服务器日期有偏移，today-1 推导必然错位）。
    """
    local = market_local_datetime(market, now_utc)
    return local.time() >= _MARKET_CLOSE_TIME.get(market, time(16, 0))
