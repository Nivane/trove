"""Helpers shared by the MCP tests (fixtures live in ``conftest.py``)."""

from __future__ import annotations

import asyncio


async def invoke(server, name: str, **args):
    """Call a registered tool's underlying function.

    ``server.call_tool`` needs a fastmcp request context, which the direct-call
    tests do not have. This is the same function fastmcp dispatches to; tools
    may be async (``ask_data``) or sync (``kb_status`` / ``list_datasources``).
    """
    tool = await server.get_tool(name)
    result = tool.fn(**args)
    if asyncio.iscoroutine(result):
        return await result
    return result


class FakeAuth:
    """最小 auth 替身:grants 表 + 用户表(``store`` 走同一个对象)。

    用户表可以留空 —— 那就等价于「这个人查不到」,会话层据此判定**没有主体**。
    """

    def __init__(self, grants_by_user: dict, users: dict | None = None,
                 topic_grants_by_user: dict | None = None):
        self._grants = grants_by_user
        self._users = users or {}
        self._topic_grants = topic_grants_by_user or {}
        self.store = self

    async def get_datasources(self, user_id):
        return list(self._grants.get(user_id, []))

    async def get_topic_grants(self, user_id):
        raw = self._topic_grants.get(user_id)
        return None if raw is None else {k: list(v) for k, v in raw.items()}

    async def get_row_grants(self, user_id):
        return None

    async def get_column_denials(self, user_id):
        return None

    async def get_user_by_id(self, uid):
        return self._users.get(uid)


class TokenAuth:
    """``resolve_token`` 替身:``"valid"`` → 用户 1,其余 → None。"""

    async def resolve_token(self, raw):
        return {"id": 1, "role": "admin"} if raw == "valid" else None


def server_with_identity(components, auth, identity, **kwargs):
    from trove.mcp.server import build_mcp_server

    return build_mcp_server({**components, "auth": auth}, identity=identity, **kwargs)


def wire_auth(components, auth):
    """把 auth 接到 session_manager 上 —— 生产里 main.py 就是这么接的。"""
    components["session_manager"]._auth = auth
    return auth
