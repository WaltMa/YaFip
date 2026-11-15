"""数据源注册表：页面展示与 API 校验的唯一来源。

新增数据源时在这里登记元数据，并在 server.build_context 中接入实现。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceMeta:
    name: str
    title: str
    description: str
    homepage: str | None = None
    requires_token: bool = True
    supports_test: bool = True
    implemented: bool = True


SOURCES: tuple[SourceMeta, ...] = (
    SourceMeta(
        name="tushare",
        title="tushare pro",
        description="A 股日 K、复权因子、市值快照（主力数据源）",
        homepage="https://tushare.pro",
    ),
    SourceMeta(
        name="akshare",
        title="akshare",
        description="港美股日 K、指数、标的列表（免费接口，无需 token）",
        homepage="https://akshare.akfamily.xyz",
        requires_token=False,
    ),
)

SOURCE_MAP: dict[str, SourceMeta] = {meta.name: meta for meta in SOURCES}


def get_source(name: str) -> SourceMeta | None:
    return SOURCE_MAP.get(name)
