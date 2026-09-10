"""ConnectorRegistry 结果缓存的身份边界。

``execute()`` 以 ``(数据源名, 归一化 SQL)`` 为键缓存只读结果(TTL 60s),让
probe / check / execute_sql 对同一 SELECT 的重复执行跳过数据库。这个优化本身
是有意的;问题在**键里的数据源名是可变的**:管理端把同名数据源重指向另一个
物理库(改 URL 重新注册)时,名字不变而库里的数据已经换了一批——缓存若不在
注册/注销时失效,答案路径(execute_sql → connectors.execute)会在 TTL 内继续
返回**旧库**的行。静默错数,且与"名字 = 身份"的直觉相悖。

注:registry 另有稳定的 ``ds_id`` 身份(deterministic ``uuid5(type:name)``),
但同名重指向时 ds_id 同样不变——设计如此,"那是同一个数据源换了地址"。所以
失效必须挂在 register / unregister 上,不能靠往键里加 ds_id。
"""

import pytest

from trove.core.types import DatasourceConfig
from trove.services.datasource.registry import ConnectorRegistry

_COUNT_SQL = "SELECT COUNT(*) AS n FROM students"


async def _register_with_rows(registry: ConnectorRegistry, rows: int):
    """注册同名数据源,建表并灌 rows 行。"""
    config = DatasourceConfig(
        name="test_db", type="sqlite",
        connection_params={"path": ":memory:"}, default=True,
    )
    adapter = await registry.register(config, set_default=True)
    await adapter.execute(
        "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, "
        "grade INTEGER, county TEXT)")
    for i in range(rows):
        await adapter.execute(
            f"INSERT INTO students (name, grade, county) "
            f"VALUES ('s{i}', {60 + i}, 'Orange')")
    return adapter


@pytest.fixture
async def registry():
    reg = ConnectorRegistry()
    yield reg
    await reg.unregister("test_db")


class TestResultCacheIdentity:
    async def test_reregister_same_name_drops_cached_rows(self, registry):
        """同名重指向另一个库 → 不得在 TTL 内返回旧库的行。

        管理端改数据源 URL 重新注册 = 同一个数据源(new ds_id 相同)换了物理
        地址;名字没变,但 ``SELECT COUNT(*)`` 的答案已经不同。
        """
        await _register_with_rows(registry, 5)
        assert (await registry.execute(_COUNT_SQL)).rows == [[5]]

        # 同身份重新注册,指向另一个(空的)库 → 灌 7 行
        await _register_with_rows(registry, 7)
        assert (await registry.execute(_COUNT_SQL)).rows == [[7]]

    async def test_unregister_then_register_drops_cached_rows(self, registry):
        """注销 → 重注册(同名、新库):旧条目必须先失效。"""
        await _register_with_rows(registry, 5)
        assert (await registry.execute(_COUNT_SQL)).rows == [[5]]

        await registry.unregister("test_db")
        await _register_with_rows(registry, 7)
        assert (await registry.execute(_COUNT_SQL)).rows == [[7]]

    async def test_result_cache_still_serves_within_a_registration(self, registry):
        """同一次注册内缓存照常生效(优化的本意,不因失效而丢失)。

        底层数据被旁路写入(adapter 直连)后,TTL 内仍返回缓存值——这是缓存
        的有意取舍(短 TTL 换重复读的省略),不是回归。
        """
        adapter = await _register_with_rows(registry, 5)
        assert (await registry.execute(_COUNT_SQL)).rows == [[5]]

        await adapter.execute(
            "INSERT INTO students (name, grade, county) VALUES ('x', 99, 'Orange')")
        assert (await registry.execute(_COUNT_SQL)).rows == [[5]]  # 命中缓存
        assert registry.result_cache_stats()["hits"] >= 1
