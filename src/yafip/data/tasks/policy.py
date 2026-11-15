"""复权策略与市场单位钩子（/）。"""
from __future__ import annotations

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
