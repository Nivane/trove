"""Apache Doris adapter — reuses the MySQL wire-protocol driver (aiomysql).

Doris exposes a MySQL-compatible protocol (Frontend query port 9030 by
default), so the adapter subclasses :class:`MySQLAdapter` and only differs
in dialect / default port / label. Schema introspection via
``information_schema``, ``SELECT VERSION()`` and ``KILL QUERY`` all work
unchanged. One deliberate difference: Doris FE does not reliably implement
the MySQL ``COM_PING`` command, so the liveness probe uses a lightweight
``SELECT 1`` instead of ``conn.ping``.
"""

from __future__ import annotations

from typing import Any

from trove.core.types import Capabilities
from trove.core.errors import DatasourceError
from trove.services.datasource.adapters.mysql import MySQLAdapter

DEFAULT_PORT = 9030


class DorisAdapter(MySQLAdapter):
    """Apache Doris database adapter via aiomysql (async, MySQL protocol)."""

    label = "Doris"
    default_port = DEFAULT_PORT
    driver_hint = "`uv sync --extra doris`"

    # 画像能力**显式收窄**:继承 MySQL 的实现,但不继承它的能力矩阵。
    # DATA_LENGTH / UPDATE_TIME 在 Doris 上是 FE 虚拟表里的列,值的行为无从
    # 验证(§8.2 B:声明只写验证过的),而 UPDATE_TIME 一旦给垃圾值就会变成
    # 对外的 data_as_of。清空 _PROFILE_COLS 之后连查都不会去查。
    profile_capabilities = frozenset({"row_count"})
    _PROFILE_COLS: tuple = ()

    # DB 侧语句超时:显式**收窄**(覆盖 MySQL 的 True)。Doris 的会话级超时
    # 语句语义未在本仓验证过 —— 声明 True 会让 MySQL 的 ``SET SESSION
    # max_execution_time`` 原样发到 Doris 上,而它认不认、认成什么,没人试过
    # (同 ``_PROFILE_COLS`` 那条纪律:声明只写验证过的)。声明 False = 一个字
    # 都不发,退回既有取消链(interrupt 仍然可用)。
    supports_statement_timeout = False

    def __init__(self, name: str = "doris", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})

    @staticmethod
    def dialect() -> str:
        return "doris"

    async def _ping_reconnect(self) -> None:
        """Probe liveness with SELECT 1 — Doris FE lacks MySQL COM_PING.

        A long-running ``trove serve`` otherwise turns every query/catalog
        call into a raw driver exception when Doris recycles the idle
        connection; the lightweight query re-opens it transparently.
        """
        try:
            cursor = await self._conn.cursor()
            try:
                await cursor.execute("SELECT 1")
                await cursor.fetchall()
            finally:
                await cursor.close()
        except Exception as e:
            raise DatasourceError(
                message=f"Doris connection lost and reconnect failed: {e}",
                datasource=self.name,
            ) from e

    async def get_capabilities(self) -> Capabilities:
        # Doris 2.x+ 支持 CTE 与窗口函数;DML 无 MySQL 式 ACID 事务。
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=False,
            supports_json_type=True,
            dialect="doris",
        )
