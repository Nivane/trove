"""register_adapter —— 内置方言自注册 + 外部注册路径真能解析。

``register_adapter()`` 此前是全仓零调用的「死接口」:函数在,但没有任何
一处代码走它,注册全靠一份硬编码 dict。现在它是**唯一**注册路径,内置 7
个方言也经它自注册——这两件事都要有断言钉住,否则哪天有人"顺手"把 dict
字面量加回来,接线又会静默断掉。
"""

from __future__ import annotations

import pytest

from trove.core.errors import DatasourceError
from trove.core.types import Capabilities, DatasourceConfig, QueryResult, SchemaInfo
from trove.services.datasource.adapters.base import DatabaseAdapter
from trove.services.datasource.registry import (
    _ADAPTER_REGISTRY,
    ConnectorRegistry,
    register_adapter,
)

#: 假方言名:不可能与内置/未来内置撞名,fixture 收尾只清自己这一条。
_FAKE_DIALECT = "fake-for-registration-test"


class _FakeAdapter(DatabaseAdapter):
    """最小可用适配器:connect 不发网络,只记账。"""

    connected_count = 0

    async def connect(self) -> None:
        type(self).connected_count += 1
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def execute(self, sql: str) -> QueryResult:
        return QueryResult(columns=["x"], rows=[[1]])

    async def get_schema(self) -> SchemaInfo:
        return SchemaInfo(tables=[])

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(dialect="fakedb")

    @staticmethod
    def dialect() -> str:
        return "fakedb"


@pytest.fixture
def fake_dialect():
    """注册假方言,收尾**清理**(全局注册表不能被测试条目泄漏污染)。"""
    _FakeAdapter.connected_count = 0
    assert _FAKE_DIALECT not in _ADAPTER_REGISTRY, "假方言名撞上了真实条目"
    register_adapter(_FAKE_DIALECT, _FakeAdapter)
    try:
        yield _FAKE_DIALECT
    finally:
        _ADAPTER_REGISTRY.pop(_FAKE_DIALECT, None)
    assert _FAKE_DIALECT not in _ADAPTER_REGISTRY


def test_builtin_dialects_are_self_registered():
    """内置 7 方言经 register_adapter() 自注册(import 即就位)。"""
    assert {
        "sqlite", "mysql", "doris", "postgres", "clickhouse", "duckdb",
        "snowflake",
    } <= set(_ADAPTER_REGISTRY)


async def test_registered_dialect_is_resolvable(fake_dialect):
    """经 register_adapter 注册的方言可被 ConnectorRegistry.prepare 解析。"""
    registry = ConnectorRegistry()
    config = DatasourceConfig(
        name="fake-source", type=_FAKE_DIALECT, connection_params={"path": ":memory:"},
    )
    adapter = await registry.prepare(config)
    try:
        assert isinstance(adapter, _FakeAdapter)
        assert adapter.name == "fake-source"
        assert _FakeAdapter.connected_count == 1
    finally:
        await adapter.disconnect()


async def test_unknown_dialect_lists_available(fake_dialect):
    """未知方言报错要列出可用方言——列的就是注册表现状(含刚注册的假方言)。"""
    registry = ConnectorRegistry()
    config = DatasourceConfig(name="ghost", type="no-such-dialect")
    with pytest.raises(DatasourceError) as ei:
        await registry.prepare(config)
    message = str(ei.value)
    assert "no-such-dialect" in message
    assert "sqlite" in message and fake_dialect in message
