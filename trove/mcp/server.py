"""Trove MCP server — 把 NL→SQL 问答能力暴露为 MCP tools。

用官方 ``fastmcp`` 高层封装。工具面:

- ``ask_data``:自然语言提问 → 答案/SQL/行数/verdict/拒绝信息。多轮
  会话用 ``session_id`` 参数复用(进程内会话注册表;缺失则新建)。
- ``list_datasources``:已连接且 KB 已初始化的数据源(用户端可见性规则)。
- ``kb_status``:数据源连接 / KB 初始化 / 语义模型文件状态。

语义优先(Phase B)天然生效:无语义模型的数据源 ask_data 会明确拒绝并
提示 /kb init;未覆盖查询 → 拒绝 + draft。``trove mcp`` 命令以 stdio
transport 启动(供 Claude Code / 其他 MCP 客户端本地挂载)。
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

_SESSION_CACHE_MAX = 200


def build_mcp_server(components: dict) -> FastMCP:
    """components(create_app_components 产物)→ 已注册工具的 FastMCP server。"""
    session_manager = components["session_manager"]
    connector_registry = components["connector_registry"]
    kb = components["kb"]
    config = components["config"]

    mcp = FastMCP("trove")

    # 进程内会话注册表:session_id → Session(ask_data 多轮复用)
    sessions: dict[str, Any] = {}

    async def _get_session(session_id: str | None) -> tuple[str, Any]:
        """返回 (effective_session_id, Session)。传入的 id 已注册 → 复用。"""
        if session_id and session_id in sessions:
            return session_id, sessions[session_id]
        session = await session_manager.start_session()
        sid = session_id or session.session_id
        sessions[sid] = session
        # 容量保护:超出后丢弃最旧(会话在 SessionStore 仍可 load_session 找回)
        while len(sessions) > _SESSION_CACHE_MAX:
            sessions.pop(next(iter(sessions)), None)
        return sid, session

    def _datasources_visible() -> list[dict[str, Any]]:
        """已注册且有语义模型的数据源(语义优先:唯一可答边界 = semantics.yml)。"""
        out: list[dict[str, Any]] = []
        for info in connector_registry.list_info():
            name = str(info.get("name") or "")
            if not name:
                continue
            try:
                has_semantics = kb.semantics_path(name).exists()
            except Exception:
                has_semantics = False
            if has_semantics:
                out.append({"name": name, "has_semantics": True})
        return out

    def _kb_status(datasource: str) -> dict[str, Any]:
        """单个数据源的连接/KB/语义模型状态。"""
        connected = False
        try:
            connected = connector_registry.is_registered(datasource)
        except Exception:
            connected = False
        initialized = False
        semantics_exists = False
        if connected:
            try:
                initialized = kb.kb_initialized(datasource)
                semantics_exists = kb.semantics_path(datasource).exists()
            except Exception:
                pass
        return {
            "datasource": datasource,
            "connected": connected,
            "kb_initialized": initialized,
            "has_semantics": semantics_exists,
            "lang": config.language if config else "en",
        }

    # ── tools ─────────────────────────────────────────────

    @mcp.tool()
    async def ask_data(
        question: str,
        datasource: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the datasource a natural-language question and get the answer.

        Returns Markdown answer, generated SQL, row count, verdict, and
        refusal info (semantic-model-first: uncovered questions are refused
        with a reason, never guessed). Pass an existing ``session_id`` for
        multi-turn context; omit for a fresh session.
        """
        question = (question or "").strip()
        if not question:
            return {"error": "question is required"}
        sid, session = await _get_session(session_id)
        try:
            state = await session_manager.ask(
                session=session, question=question, datasource=datasource,
            )
        except Exception as e:
            return {"session_id": sid, "error": f"ask failed: {e}"}
        return {
            "session_id": sid,
            "answer": state.final_response,
            "sql": state.sql,
            "row_count": state.row_count,
            "verdict": state.verdict,
            "datasource": state.datasource,
            "no_model": state.no_model,
            "refusal": state.refusal,
        }

    @mcp.tool()
    def list_datasources() -> dict[str, Any]:
        """List datasources that are connected AND have a semantic model —
        the ones answerable via ask_data (semantic-first: no model = not answerable)."""
        return {"datasources": _datasources_visible()}

    @mcp.tool()
    def kb_status(datasource: str) -> dict[str, Any]:
        """Return connection / KB-init / semantic-model status for one datasource —
        use to decide whether to initialize the KB (kb init) first."""
        if not (datasource or "").strip():
            return {"error": "datasource is required"}
        return _kb_status(datasource.strip())

    return mcp
