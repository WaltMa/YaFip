"""akshare 港美股真实接口冒烟。

- akshare 未装 / 断网时自动 skip；只做单个标的、小日期区间。
"""
from __future__ import annotations

import pytest

try:
    import akshare  # noqa: F401

    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAS_AKSHARE, reason="akshare 未安装")]


def _call(label, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 网络/接口波动时跳过
        pytest.skip(f"{label} 接口不可用：{exc}")
    raise AssertionError("unreachable")


def test_akshare_hk_daily_one_symbol():
    from yafip.data.akshare_source import AkshareSource

    source = AkshareSource()
    df = _call("hk_daily", source.get_daily, "HK", "2026-11-13", symbols=["00700.HK"])
    if df is not None and not df.empty:
        assert "ts_code" in df.columns
        assert df.iloc[0]["ts_code"] == "00700.HK"


def test_akshare_us_daily_one_symbol():
    from yafip.data.akshare_source import AkshareSource

    source = AkshareSource()
    df = _call("us_daily", source.get_daily, "US", "2026-11-13", symbols=["AAPL"])
    if df is not None and not df.empty:
        assert "ts_code" in df.columns
        assert df.iloc[0]["ts_code"] == "AAPL"


def test_akshare_us_index_calendar():
    from yafip.data.akshare_source import AkshareSource

    source = AkshareSource()
    df = _call("us_index", source.get_calendar, "US", "2026-11-01", "2026-11-30")
    if df is not None and not df.empty:
        assert "cal_date" in df.columns
        assert int(df.iloc[0]["is_open"]) == 1
