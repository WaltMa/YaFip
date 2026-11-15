"""DataService：业务层入口，把业务意图翻译成 TaskSpec 并提交。

统一 `submit()` 经执行器注册中心校验后提交（POST /api/tasks 主路径）；
`quality.scan` 为辅助任务，不占 5 类主任务，保留直连提交。
"""
from __future__ import annotations

from typing import Any

from yafip.config import ConfigService
from yafip.core.task_center import TaskCenter, TaskSpec
from yafip.data.executors import ExecutorRegistry
from yafip.data.market import MARKET_CN_A

HANDLER_QUALITY = "data.quality"


def _lock(task_type: str, market: str | None) -> str:
    return f"{task_type}:{market or '*'}"


class DataService:
    def __init__(
        self,
        tasks: TaskCenter,
        registry: ExecutorRegistry,
        config: ConfigService,
    ):
        self.tasks = tasks
        self.registry = registry
        self.config = config

    # -- 统一提交入口（注册中心校验，POST /api/tasks 主路径） ---------------
    def submit(
        self,
        task_type: str,
        executor: str,
        market: str,
        params: dict[str, Any],
        source: str = "manual",
    ) -> str:
        spec = self.registry.validate_submission(task_type, executor, market, params)
        payload = dict(params or {})
        payload["market"] = market
        payload["executor"] = executor  # executor 进入任务上下文，单元 manifest 记录真实执行器
        task_spec = TaskSpec(
            task_type=task_type,
            executor=executor,
            market=market,
            source=source,
            params=payload,
            handler=spec.handler,
            lock_key=_lock(task_type, market),
        )
        return self.tasks.submit(task_spec)

    # -- 质量扫描（辅助任务，直连提交） -------------------------------------
    def create_quality_scan(self, params: dict[str, Any]) -> str:
        payload = dict(params or {})
        market = str(payload.get("market") or MARKET_CN_A)
        payload["market"] = market
        spec = TaskSpec(
            task_type="quality.scan",
            executor="tushare.quality.scan",
            market=market,
            source="manual",
            params=payload,
            handler=HANDLER_QUALITY,
            lock_key=_lock("quality.scan", market),
        )
        return self.tasks.submit(spec)


def register_handlers(tasks: TaskCenter, fetcher, whitelist, quality) -> None:
    # 历史任务（handler=data.fetch / data.whitelist）retry 兼容；新任务走 tasks.*
    tasks.register("data.fetch", fetcher.run)
    tasks.register("data.whitelist", whitelist.recompute_handler)
    tasks.register(HANDLER_QUALITY, quality.scan)

