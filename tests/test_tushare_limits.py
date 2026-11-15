from __future__ import annotations

from __future__ import annotations

import pytest

from yafip.config import ConfigService
from yafip.data.base import DataSourceError
from yafip.data.repository import Repository
from yafip.data.tushare_source import TushareSource
from conftest import make_db


def test_parse_rate_limit_minutes():
    message = "抱歉，您访问接口(trade_cal)频率超限(1次/分钟)，具体频次详情：https://tushare.pro/document/1?doc_id=108"
    assert TushareSource._parse_rate_limit(message) == (1, 60)


def test_parse_rate_limit_hours():
    message = "抱歉，您访问接口(daily_basic)频率超限(1次/小时)，具体频次详情"
    assert TushareSource._parse_rate_limit(message) == (1, 3600)


def test_parse_rate_limit_with_value():
    assert TushareSource._parse_rate_limit("频率超限(2次/30秒)") == (2, 30)


def test_parse_rate_limit_none():
    assert TushareSource._parse_rate_limit("permission denied") is None


def test_negotiated_rate_not_clobbered_by_config(tmp_path):
    """回归：_call 解析出服务端真实限额后，循环顶部 _limiter_for 不得用配置值覆盖。"""
    db = make_db(tmp_path)
    config = ConfigService(db)
    repo = Repository(db)
    source = TushareSource(repo, config)
    repo.save_datasource("tushare", token="fake-token", enabled=True)

    def limited(**kwargs):
        raise Exception("抱歉，您访问接口(daily)频率超限(1次/分钟)，具体频次详情")

    with pytest.raises(DataSourceError):
        source._call(limited, "daily", wait_on_limit=False)

    # 协商出的 1 次/分钟被记住；配置默认 120 次/分钟不再覆盖
    limiter = source._limiter_for("daily")
    assert (limiter.limit, limiter.window_sec) == (1, 60.0)
    assert source._server_rates["daily"] == (1, 60.0)

    # 再次触发同一限频错误：协商值不变
    with pytest.raises(DataSourceError):
        source._call(limited, "daily", wait_on_limit=False)
    assert source._server_rates["daily"] == (1, 60.0)
