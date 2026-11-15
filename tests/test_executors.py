from __future__ import annotations

import pytest

from yafip.data.executors import (
    TASK_TYPES,
    ExecutorError,
    ExecutorRegistry,
    ExecutorSpec,
    build_default_registry,
)
from yafip.data.market import MARKET_CN_A, MARKET_HK


def test_task_types_and_visibility():
    assert TASK_TYPES == (
        "meta.sync_all", "meta.sync_symbols", "whitelist.update",
        "bars.incremental", "bars.range",
    )
    reg = build_default_registry()
    names = [s.name for s in reg.list()]
    assert len(names) == 8  # tushare ×5 + akshare ×3（已实装）
    assert "akshare.bars.incremental" in names
    assert all(s.implemented for s in reg.list())


def test_registry_validation_cases():
    reg = build_default_registry()
    spec = reg.validate_submission("bars.range", "tushare.bars.range", MARKET_CN_A,
                                   {"start": "2025-01-01", "end": "2025-01-31"})
    assert spec.task_type == "bars.range"

    # 串类型
    with pytest.raises(ExecutorError, match="串类型"):
        reg.validate_submission("bars.incremental", "tushare.bars.range", MARKET_CN_A, {})
    # 未知执行器
    with pytest.raises(ExecutorError, match="未知执行器"):
        reg.validate_submission("bars.range", "nope.bars.range", MARKET_CN_A, {})
    # 市场越界
    with pytest.raises(ExecutorError, match="不适用市场"):
        reg.validate_submission("bars.range", "tushare.bars.range", MARKET_HK,
                                {"start": "2025-01-01", "end": "2025-01-31"})
    # 未实现执行器：当前全部实装；用未知执行器代替验证拒绝
    with pytest.raises(ExecutorError, match="未知执行器"):
        reg.validate_submission("meta.sync_all", "bogus.meta.sync_all", MARKET_HK, {})
    # 缺必填参数
    with pytest.raises(ExecutorError, match="缺少必填参数"):
        reg.validate_submission("bars.range", "tushare.bars.range", MARKET_CN_A, {})
    # 非法日期
    with pytest.raises(ExecutorError, match="YYYY-MM-DD"):
        reg.validate_submission("bars.range", "tushare.bars.range", MARKET_CN_A,
                                {"start": "20250101", "end": "2025-01-31"})


def test_symbol_list_validation():
    reg = build_default_registry()
    with pytest.raises(ExecutorError, match="非法 symbol"):
        reg.validate_submission(
            "meta.sync_symbols", "tushare.meta.sync_symbols", MARKET_CN_A,
            {"symbols": ["600519", "000001.SZ"]},
        )
    spec = reg.validate_submission(
        "meta.sync_symbols", "tushare.meta.sync_symbols", MARKET_CN_A,
        {"symbols": ["600519.SH", "000001.SZ"]},
    )
    assert spec.name == "tushare.meta.sync_symbols"


def test_number_range_validation():
    reg = build_default_registry()
    with pytest.raises(ExecutorError, match="不能小于"):
        reg.validate_submission(
            "whitelist.update", "tushare.whitelist.update", MARKET_CN_A,
            {"metric": "total_mv", "op": ">", "value": 0},
        )
    spec = reg.validate_submission(
        "whitelist.update", "tushare.whitelist.update", MARKET_CN_A,
        {"metric": "total_mv", "op": ">", "value": 1000000},
    )
    assert spec.task_type == "whitelist.update"


def test_duplicate_register_rejected():
    reg = ExecutorRegistry()
    reg.register(ExecutorSpec(name="x.y", task_type="bars.range", source="x",
                              markets=(MARKET_CN_A,), handler="h"))
    with pytest.raises(ExecutorError, match="已注册"):
        reg.register(ExecutorSpec(name="x.y", task_type="bars.range", source="x",
                                  markets=(MARKET_CN_A,), handler="h"))
    with pytest.raises(ExecutorError, match="未知任务类型"):
        reg.register(ExecutorSpec(name="x.bad", task_type="strategy.train", source="x",
                                  markets=(MARKET_CN_A,), handler="h"))


def test_list_filters():
    reg = build_default_registry()
    cn = [s.name for s in reg.list(market=MARKET_CN_A)]
    assert len(cn) == 5
    hk = [s.name for s in reg.list(market=MARKET_HK)]
    assert hk == [
        "akshare.bars.incremental",
        "akshare.bars.range",
        "akshare.meta.sync_all",
    ]
    by_type = [s.name for s in reg.list(task_type="whitelist.update")]
    assert by_type == ["tushare.whitelist.update"]
    params = reg.get("tushare.bars.range").params_schema
    assert set(params) == {"start", "end", "symbols"}
    # 港美股执行器可正常校验（实装）
    spec = reg.validate_submission("bars.range", "akshare.bars.range", MARKET_HK,
                                   {"start": "2025-01-01", "end": "2025-01-31"})
    assert spec.handler == "tasks_ak.bars_range"
