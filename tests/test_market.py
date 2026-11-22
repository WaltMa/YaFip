from __future__ import annotations

import pytest

from yafip.data.market import (
    MARKET_CN_A,
    MARKET_HK,
    MARKET_US,
    MarketError,
    display_symbol,
    infer_asset_type,
    market_of_symbol,
    normalize_symbol,
    validate_symbol,
)


def test_markets_registry():
    from yafip.data.market import MARKETS, MARKET_LABELS

    assert MARKETS == (MARKET_CN_A, MARKET_HK, MARKET_US)
    assert set(MARKET_LABELS) == set(MARKETS)


def test_normalize_cn_a_keeps_as_is():
    assert normalize_symbol(MARKET_CN_A, "600519.SH") == "600519.SH"
    assert normalize_symbol(MARKET_CN_A, "513100.sh") == "513100.SH"


def test_normalize_hk_appends_suffix():
    assert normalize_symbol(MARKET_HK, "00700") == "00700.HK"
    assert normalize_symbol(MARKET_HK, "00700.hk") == "00700.HK"
    assert normalize_symbol(MARKET_HK, "800700.HK") == "800700.HK"
    # 未补零代码：Sina 需要 00700 而非 700/0700（修复前 700 → 700.HK 匹配不上源侧）
    assert normalize_symbol(MARKET_HK, "700") == "00700.HK"
    assert normalize_symbol(MARKET_HK, "0700") == "00700.HK"
    assert normalize_symbol(MARKET_HK, "700.HK") == "00700.HK"


def test_normalize_cn_a_rejects_bare_code():
    # 裸 600519 无后缀会被源侧 ts_code（600519.SH）匹配不上而全被过滤，必须直接抛错
    with pytest.raises(MarketError, match="格式应为"):
        normalize_symbol(MARKET_CN_A, "600519")
    with pytest.raises(MarketError, match="格式应为"):
        normalize_symbol(MARKET_CN_A, "600519.SHH")


def test_normalize_us_keeps_ticker():
    assert normalize_symbol(MARKET_US, "AAPL") == "AAPL"
    assert normalize_symbol(MARKET_US, "qqq") == "QQQ"
    assert normalize_symbol(MARKET_US, "^vix") == "^VIX"


def test_normalize_rejects_unknown_market():
    with pytest.raises(MarketError):
        normalize_symbol("JP", "1234")


def test_validate_symbol_formats():
    validate_symbol(MARKET_CN_A, "600519.SH")
    validate_symbol(MARKET_CN_A, "000688.SH")
    with pytest.raises(MarketError):
        validate_symbol(MARKET_CN_A, "600519")
    with pytest.raises(MarketError):
        validate_symbol(MARKET_CN_A, "AAPL")

    validate_symbol(MARKET_HK, "00700.HK")
    with pytest.raises(MarketError):
        validate_symbol(MARKET_HK, "00700")

    validate_symbol(MARKET_US, "AAPL")
    validate_symbol(MARKET_US, "^VIX")
    validate_symbol(MARKET_US, "BRK.B")
    with pytest.raises(MarketError):
        validate_symbol(MARKET_US, "TOO-LONG-TICKER-NAME")


def test_display_symbol():
    assert display_symbol(MARKET_CN_A, "600519.SH") == "CN_A:600519.SH"


def test_market_of_symbol_guesses():
    assert market_of_symbol("00700.HK") == MARKET_HK
    assert market_of_symbol("600519.SH") == MARKET_CN_A
    assert market_of_symbol("AAPL") == MARKET_US
    assert market_of_symbol("^VIX") == MARKET_US


def test_infer_asset_type_cn_a():
    # 股票
    assert infer_asset_type(MARKET_CN_A, "600519.SH") == "STOCK"
    assert infer_asset_type(MARKET_CN_A, "000001.SZ") == "STOCK"
    assert infer_asset_type(MARKET_CN_A, "688981.SH") == "STOCK"
    assert infer_asset_type(MARKET_CN_A, "832971.BJ") == "STOCK"
    # 指数
    assert infer_asset_type(MARKET_CN_A, "000688.SH") == "INDEX"
    assert infer_asset_type(MARKET_CN_A, "000001.SH") == "INDEX"
    assert infer_asset_type(MARKET_CN_A, "399001.SZ") == "INDEX"
    # ETF（51/56/58 SH；15/16 SZ）
    assert infer_asset_type(MARKET_CN_A, "513100.SH") == "ETF"
    assert infer_asset_type(MARKET_CN_A, "588000.SH") == "ETF"
    assert infer_asset_type(MARKET_CN_A, "159915.SZ") == "ETF"
    # 非法/缺失格式回退 STOCK
    assert infer_asset_type(MARKET_CN_A, "600519") == "STOCK"
    assert infer_asset_type(MARKET_CN_A, "") == "STOCK"


def test_infer_asset_type_hk_us_default_stock():
    assert infer_asset_type(MARKET_HK, "00700.HK") == "STOCK"
    assert infer_asset_type(MARKET_HK, "800700.HK") == "STOCK"  # 指数待源提供类型
    assert infer_asset_type(MARKET_US, "AAPL") == "STOCK"
    assert infer_asset_type(MARKET_US, "QQQ") == "STOCK"  # ETF 待源提供类型
    assert infer_asset_type(MARKET_US, "^VIX") == "STOCK"
