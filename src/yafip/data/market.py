"""市场常量与符号规范化。

统一标识 `(market, symbol)` 为业务主键；规范化只发生在 executor 边界，
内部一律纯字符串，不引入数字 ID。

市场
- CN_A：A 股，symbol 同展示（600519.SH / 513100.SH / 000688.SH），ETF/指数复用 A 股日历
- HK：港股，symbol 补 `.HK` 后缀（00700.HK / 800700.HK）
- US：美股，symbol 原样（AAPL / QQQ / ^VIX）
"""
from __future__ import annotations

import re

MARKET_CN_A = "CN_A"
MARKET_HK = "HK"
MARKET_US = "US"

MARKETS: tuple[str, ...] = (MARKET_CN_A, MARKET_HK, MARKET_US)

MARKET_LABELS: dict[str, str] = {
    MARKET_CN_A: "A股",
    MARKET_HK: "港股",
    MARKET_US: "美股",
}

# A 股代码格式：6 位数字 + 交易所后缀（SH/SZ/BJ），ETF/指数/股票共用
_CN_A_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
# 港股：5~6 位数字 + .HK
_HK_RE = re.compile(r"^\d{4,6}\.HK$")
# 美股：字母/数字/点（ticker），VIX 用 ^VIX 前缀形式
_US_RE = re.compile(r"^[A-Z0-9.\-^]{1,10}$")


class MarketError(ValueError):
    code = "6001"


def is_market(market: str) -> bool:
    return market in MARKETS


def require_market(market: str) -> str:
    if not is_market(market):
        raise MarketError(f"未知市场：{market}（可用 {', '.join(MARKETS)}）")
    return market


def normalize_symbol(market: str, raw: str) -> str:
    """把数据源原始符号规范化为内部 symbol；不做市场猜测。"""
    require_market(market)
    text = str(raw or "").strip().upper()
    if not text:
        raise MarketError(f"{market} 标的名称为空")
    if market == MARKET_CN_A:
        return text  # 600519.SH 原样
    if market == MARKET_HK:
        # 00700 / 00700.HK → 00700.HK（补后缀）
        return text if text.endswith(".HK") else f"{text}.HK"
    # US：原样（AAPL / QQQ / ^VIX）
    return text


def validate_symbol(market: str, symbol: str) -> str:
    """校验内部 symbol 格式；不合法抛 MarketError。"""
    require_market(market)
    text = str(symbol or "").strip().upper()
    if market == MARKET_CN_A and not _CN_A_RE.match(text):
        raise MarketError(f"A 股 symbol 格式应为 600519.SH / 513100.SH / 000688.SH，收到：{symbol}")
    if market == MARKET_HK and not _HK_RE.match(text):
        raise MarketError(f"港股 symbol 格式应为 00700.HK / 800700.HK，收到：{symbol}")
    if market == MARKET_US and not _US_RE.match(text):
        raise MarketError(f"美股 symbol 格式应为 AAPL / QQQ / ^VIX，收到：{symbol}")
    return text


def display_symbol(market: str, symbol: str) -> str:
    """展示层统一 market + symbol 拼接，避免跨市场同名歧义。"""
    require_market(market)
    return f"{market}:{symbol}"


def market_of_symbol(symbol: str) -> str:
    """按 symbol 后缀猜测市场（仅用于旧数据/展示兜底，不入库逻辑）。"""
    text = str(symbol or "").strip().upper()
    if text.endswith(".HK"):
        return MARKET_HK
    if _CN_A_RE.match(text):
        return MARKET_CN_A
    if text.startswith("^") or _US_RE.match(text):
        return MARKET_US
    raise MarketError(f"无法按 symbol 推断市场：{symbol}")


def infer_asset_type(market: str, symbol: str) -> str:
    """按市场代码规则推断资产类型（executor 边界，纯函数）。

    - CN_A 确定性规则：SH 000/880/888/899/999→INDEX、51/56/58→ETF；
      SZ 399→INDEX、15/16→ETF；其余（含 BJ）→STOCK；
    - HK/US 无代码规则：默认 STOCK（指数/ETF 待数据源接入后由源显式提供
      asset_type，本函数不猜测）。
    """
    require_market(market)
    text = str(symbol or "").strip().upper()
    if market != MARKET_CN_A:
        return "STOCK"
    if "." not in text:
        return "STOCK"
    code, exchange = text.split(".", 1)
    if not (code.isdigit() and len(code) == 6):
        return "STOCK"
    if exchange == "SH":
        if code.startswith(("000", "880", "888", "899", "999")):
            return "INDEX"
        if code.startswith(("51", "56", "58")):
            return "ETF"
    elif exchange == "SZ":
        if code.startswith("399"):
            return "INDEX"
        if code.startswith(("15", "16")):
            return "ETF"
    return "STOCK"
