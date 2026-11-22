"""执行器注册中心。

一个 executor = 一个 task_type + 一个数据源 + 一组适用市场 + 一个 params_schema。
注册表是唯一真相：驱动 Web 动态表单与提交校验。
- 提交校验不满足直接 400，不进任务队列；
- `implemented=False` 的执行器不出现在可选列表，提交也直接拒绝；
- 一个执行器的 task_type 唯一（"每个执行器的任务类型仅有 1 个"）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yafip.data.market import MARKET_CN_A, MARKET_HK, MARKET_US, validate_symbol

TASK_TYPES: tuple[str, ...] = (
    "meta.sync_all",
    "meta.sync_symbols",
    "whitelist.update",
    "bars.incremental",
    "bars.range",
)

# 任务类型中文名（表单 / 展示用）
TASK_TYPE_LABELS: dict[str, str] = {
    "meta.sync_all": "元数据全量同步",
    "meta.sync_symbols": "指定标的元数据同步",
    "whitelist.update": "白名单更新",
    "bars.incremental": "日K增量同步",
    "bars.range": "日K指定区间同步",
}


class ExecutorError(ValueError):
    code = "3001"
    http_status = 400


# ---------------------------------------------------------------------------
# params_schema 构造助手
# ---------------------------------------------------------------------------

def _schema_symbols(*, required: bool = False) -> dict:
    return {
        "symbols": {
            "key": "symbols",
            "label": "标的列表",
            "type": "symbol_list",
            "required": required,
            "default": [],
            "help": "按市场输入 symbol（如 600519.SH）；留空则使用白名单",
        }
    }


def _schema_daterange(*, symbols: bool = True) -> dict:
    schema: dict[str, Any] = {
        "start": {
            "key": "start", "label": "开始日期", "type": "date", "required": True,
            "default": "", "help": "YYYY-MM-DD",
        },
        "end": {
            "key": "end", "label": "结束日期", "type": "date", "required": True,
            "default": "", "help": "YYYY-MM-DD",
        },
    }
    if symbols:
        schema.update(_schema_symbols(required=False))
    return schema


def _schema_whitelist_rule() -> dict:
    return {
        "metric": {
            "key": "metric", "label": "指标", "type": "enum", "required": True,
            "options": ["total_mv"], "default": "total_mv", "help": "市值（万元）",
        },
        "op": {
            "key": "op", "label": "比较符", "type": "enum", "required": True,
            "options": [">"], "default": ">", "help": "本期仅支持大于",
        },
        "value": {
            "key": "value", "label": "门槛（万元）", "type": "number", "required": True,
            "min": 1, "default": 500000, "help": "如 100 亿 = 1000000 万元",
        },
    }


# ---------------------------------------------------------------------------
# ExecutorSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutorSpec:
    name: str                 # "tushare.bars.incremental"
    task_type: str            # 唯一绑定：一个执行器只对应 1 个任务类型
    source: str               # tushare / akshare
    markets: tuple[str, ...]  # 适用市场
    handler: str              # TaskCenter 注册名，落 task.handler
    params_schema: dict = field(default_factory=dict)
    implemented: bool = True

    @property
    def title(self) -> str:
        return f"{self.source} · {TASK_TYPE_LABELS.get(self.task_type, self.task_type)}"


class ExecutorRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ExecutorSpec] = {}

    def register(self, spec: ExecutorSpec) -> None:
        if spec.name in self._specs:
            raise ExecutorError(f"执行器已注册：{spec.name}")
        if spec.task_type not in TASK_TYPES:
            raise ExecutorError(f"未知任务类型：{spec.task_type}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ExecutorSpec | None:
        return self._specs.get(name)

    def list(
        self,
        *,
        market: str | None = None,
        task_type: str | None = None,
        implemented_only: bool = True,
    ) -> list[ExecutorSpec]:
        result = []
        for spec in self._specs.values():
            if implemented_only and not spec.implemented:
                continue
            if market and market not in spec.markets:
                continue
            if task_type and spec.task_type != task_type:
                continue
            result.append(spec)
        return sorted(result, key=lambda s: s.name)

    # ------------------------------------------------------------------
    def validate_submission(
        self,
        task_type: str,
        executor: str,
        market: str,
        params: dict[str, Any],
    ) -> ExecutorSpec:
        """提交校验：任一不满足直接抛 ExecutorError（400），不进任务队列。"""
        spec = self.get(executor)
        if spec is None:
            raise ExecutorError(f"未知执行器：{executor}")
        if spec.task_type != task_type:
            raise ExecutorError(f"执行器串类型：{executor} 的任务类型是 {spec.task_type}，不是 {task_type}")
        if not spec.implemented:
            raise ExecutorError(f"执行器未实现：{executor}")
        if market not in spec.markets:
            raise ExecutorError(f"执行器 {executor} 不适用市场 {market}（适用 {', '.join(spec.markets)}）")
        self._validate_params(spec, market, params)
        return spec

    @staticmethod
    def _validate_params(spec: ExecutorSpec, market: str, params: dict[str, Any]) -> None:
        payload = dict(params or {})
        for key, meta in (spec.params_schema or {}).items():
            value = payload.get(key)
            if meta.get("required") and value in (None, "", []):
                raise ExecutorError(f"缺少必填参数：{key}")
            if value in (None, ""):
                continue
            _type = meta["type"]
            if _type == "number":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ExecutorError(f"参数 {key} 必须是数字，收到：{value}") from None
                if not math.isfinite(value):
                    # nan/inf 能通过大小比较（nan<min 恒 False），会让规则 0 命中但任务成功
                    raise ExecutorError(f"参数 {key} 必须是有限数字，收到：{value}")
                if meta.get("min") is not None and value < meta["min"]:
                    raise ExecutorError(f"参数 {key} 不能小于 {meta['min']}")
                if meta.get("max") is not None and value > meta["max"]:
                    raise ExecutorError(f"参数 {key} 不能大于 {meta['max']}")
            elif _type == "date":
                text = str(value)
                try:
                    datetime.strptime(text, "%Y-%m-%d")
                except ValueError:
                    raise ExecutorError(f"参数 {key} 必须是合法日期 YYYY-MM-DD，收到：{text}") from None
            elif _type == "enum":
                if value not in meta.get("options", []):
                    raise ExecutorError(f"参数 {key} 取值应为 {'/'.join(meta['options'])}，收到：{value}")
            elif _type == "symbol_list":
                for symbol in value:
                    try:
                        validate_symbol(market, str(symbol))
                    except Exception as exc:
                        raise ExecutorError(f"参数 {key} 含非法 symbol：{symbol}（{exc}）") from exc
            # string 类型不做额外约束


def build_default_registry() -> ExecutorRegistry:
    """A 股 tushare 5 类 + 港美股 akshare 3 类（实装）。"""
    registry = ExecutorRegistry()
    # 港美股 akshare（实装；whitelist.update 无市值数据源不注册）
    registry.register(ExecutorSpec(
        name="akshare.meta.sync_all", task_type="meta.sync_all", source="akshare",
        markets=(MARKET_HK, MARKET_US), handler="tasks_ak.meta_sync_all",
    ))
    registry.register(ExecutorSpec(
        name="akshare.bars.incremental", task_type="bars.incremental", source="akshare",
        markets=(MARKET_HK, MARKET_US), handler="tasks_ak.bars_incremental",
    ))
    registry.register(ExecutorSpec(
        name="akshare.bars.range", task_type="bars.range", source="akshare",
        markets=(MARKET_HK, MARKET_US), handler="tasks_ak.bars_range",
        params_schema=_schema_daterange(symbols=True),
    ))
    # A 股 tushare 5 类
    registry.register(ExecutorSpec(
        name="tushare.meta.sync_all", task_type="meta.sync_all", source="tushare",
        markets=(MARKET_CN_A,), handler="tasks.meta_sync_all",
    ))
    registry.register(ExecutorSpec(
        name="tushare.meta.sync_symbols", task_type="meta.sync_symbols", source="tushare",
        markets=(MARKET_CN_A,), handler="tasks.meta_sync_symbols",
        params_schema=_schema_symbols(required=True),
    ))
    registry.register(ExecutorSpec(
        name="tushare.whitelist.update", task_type="whitelist.update", source="tushare",
        markets=(MARKET_CN_A,), handler="tasks.whitelist_update",
        params_schema=_schema_whitelist_rule(),
    ))
    registry.register(ExecutorSpec(
        name="tushare.bars.incremental", task_type="bars.incremental", source="tushare",
        markets=(MARKET_CN_A,), handler="tasks.bars_incremental",
    ))
    registry.register(ExecutorSpec(
        name="tushare.bars.range", task_type="bars.range", source="tushare",
        markets=(MARKET_CN_A,), handler="tasks.bars_range",
        params_schema=_schema_daterange(symbols=True),
    ))
    return registry
