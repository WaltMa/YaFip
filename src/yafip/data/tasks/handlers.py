"""任务 handler 装配（/）。

tushare（A 股）与 akshare（HK/US）分别绑定独立 handler 与 Fetcher 实例；
whitelist.update 复用 WhitelistService（读表）；港美股无市值数据源不注册。
"""
from __future__ import annotations

from typing import Callable

from yafip.data.tasks.meta import make_meta_handlers


def build_task_handlers(
    *,
    config,
    repo,
    source,
    staging,
    fetcher,
    whitelist,
    akshare_source=None,
    akshare_fetcher=None,
) -> dict[str, Callable]:
    handlers: dict[str, Callable] = {}

    handlers.update(make_meta_handlers(config=config, repo=repo, source=source, staging=staging))

    def whitelist_update(ctx, params: dict) -> dict:
        return whitelist.recompute_handler(ctx, params)

    def bars_incremental(fetcher_instance):
        def _run(ctx, params: dict) -> dict:
            payload = dict(params)
            payload["mode"] = "incremental"
            return fetcher_instance.run(ctx, payload)
        return _run

    def bars_range(fetcher_instance):
        def _run(ctx, params: dict) -> dict:
            payload = dict(params)
            payload["mode"] = "range"
            return fetcher_instance.run(ctx, payload)
        return _run

    handlers["tasks.whitelist_update"] = whitelist_update
    handlers["tasks.bars_incremental"] = bars_incremental(fetcher)
    handlers["tasks.bars_range"] = bars_range(fetcher)

    if akshare_source is not None:
        handlers.update(make_meta_handlers(
            config=config, repo=repo, source=akshare_source, staging=staging, prefix="tasks_ak",
        ))
        if akshare_fetcher is not None:
            handlers["tasks_ak.bars_incremental"] = bars_incremental(akshare_fetcher)
            handlers["tasks_ak.bars_range"] = bars_range(akshare_fetcher)
    return handlers
