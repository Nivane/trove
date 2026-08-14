"""Session manager — coordinates the full query lifecycle.

This is the high-level orchestrator that:
1. Loads or creates a session
2. Runs a compiled LangGraph workflow (thread_id = session_id)
3. Saves results back to the session
4. Handles compaction

It ties together SessionStore, compiled graphs, and LLMGateway
into a single coherent API.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from trove.core.types import (
    Message,
    Session,
)
from trove.core.config import AgentConfig
from trove.core.errors import SessionError
from trove.core.logging import get_logger
from trove.storage.session_store import SessionStore
from trove.llm.gateway import LLMGateway
from trove.llm.token_counter import TokenCounter
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

DEFAULT_WORKFLOW = "reflection"


class SessionManager:
    """High-level orchestration of conversations and queries.

    Usage:
        manager = SessionManager(config, store, graphs, llm)
        session = await manager.start_session()
        state = await manager.ask(session, "How many users?")
    """

    def __init__(
        self,
        config: AgentConfig,
        session_store: SessionStore,
        graphs: dict[str, Any],
        llm_gateway: LLMGateway,
    ):
        self.config = config
        self._store = session_store
        self._graphs = graphs
        self._llm = llm_gateway
        self._token_counter = TokenCounter()

    # ── Session lifecycle ────────────────────────────────

    async def start_session(
        self,
        project_cwd: str = ".",
        user_id: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session.

        Args:
            project_cwd: Project directory for session sharding.
            user_id: User identifier.
            metadata: Optional metadata.

        Returns:
            A new Session.
        """
        session = await self._store.create_session(
            project_cwd=project_cwd,
            user_id=user_id,
            metadata=metadata or {},
        )
        logger.info("Started session: %s", session.session_id)
        return session

    async def load_session(
        self,
        session_id: str,
        project_cwd: str = ".",
    ) -> Session:
        """Load an existing session.

        Args:
            session_id: The session to load.
            project_cwd: Project directory.

        Returns:
            The loaded Session.

        Raises:
            SessionError: If not found.
        """
        return await self._store.load_session(session_id, project_cwd)

    async def save_session(self, session: Session) -> None:
        """Persist the current session state."""
        await self._store.save_session(session)

    async def list_sessions(self, project_cwd: str = ".") -> list[dict[str, Any]]:
        """List all sessions for a project."""
        return await self._store.list_sessions(project_cwd)

    async def delete_session(self, session_id: str, project_cwd: str = ".") -> bool:
        """Delete a session and its stored data."""
        return await self._store.delete_session(session_id, project_cwd)

    async def compact_session(
        self,
        session: Session,
        keep_recent: int = 3,
    ) -> Session:
        """Compact a session by summarizing old messages.

        Uses LLM to generate a conversation summary, then replaces
        old messages with the summary while keeping recent ones.

        Args:
            session: Session to compact.
            keep_recent: Number of recent message pairs to preserve.

        Returns:
            The compacted session.
        """
        if len(session.messages) <= keep_recent * 2:
            logger.debug("Session too short to compact")
            return session

        # Build prompt for LLM summarization
        old_messages = session.messages[: -keep_recent * 2]
        conversation = "\n".join(
            f"[{m.role}]: {m.content[:500]}" for m in old_messages
        )

        prompt = (
            "Summarize this conversation, preserving key facts, "
            "SQL queries generated, data insights discovered, and "
            "any important decisions or corrections made.\n\n"
            f"{conversation}\n\n"
            "Summary:"
        )

        try:
            summary = await self._llm.chat(
                model=self.config.target or "openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
            )
            return await self._store.compact_session(
                session, summary, keep_recent=keep_recent,
            )
        except Exception as e:
            logger.warning("Compaction failed: %s", e)
            return session

    # ── Query ────────────────────────────────────────────

    def _get_graph(self, workflow_name: str):
        """Look up a compiled graph by workflow name.

        Raises:
            KeyError: If the workflow is unknown.
        """
        if workflow_name not in self._graphs:
            raise KeyError(
                f"Workflow '{workflow_name}' not found. "
                f"Available: {list(self._graphs.keys())}"
            )
        return self._graphs[workflow_name]

    def _thread_config(self, session: Session) -> dict[str, Any]:
        """RunnableConfig mapping session_id → checkpointer thread_id."""
        return {"configurable": {"thread_id": session.session_id}}

    async def ask(
        self,
        session: Session,
        question: str,
        workflow_name: str = DEFAULT_WORKFLOW,
    ) -> WorkflowState:
        """Process a natural language question through a compiled graph.

        Args:
            session: Current session.
            question: The user's natural language question.
            workflow_name: Which graph to run ("reflection", "fixed", "empty").

        Returns:
            The final WorkflowState (final_response, sql, row_count, verdict, ...).

        Raises:
            KeyError: If the workflow is unknown.
        """
        graph = self._get_graph(workflow_name)

        # Create user message
        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        state = WorkflowState(session_id=session.session_id, question=question)
        final = WorkflowState.model_validate(
            await graph.ainvoke(state, self._thread_config(session))
        )

        await self._record_exchange(session, workflow_name, final)
        return final

    async def ask_stream(
        self,
        session: Session,
        question: str,
        workflow_name: str = DEFAULT_WORKFLOW,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a query response as graph events.

        Yields events:
          {"type": "thought"|"sql"|"result"|"done"|"error", "node": ..., ...}
        The terminal event (done or error) carries a "summary" dict with
        the final state essentials (sql, row_count, verdict, error,
        final_response, ...) for non-streaming consumers like --print.
        """
        yield {"type": "thought", "node": "start", "content": f"Processing: {question[:80]}..."}

        try:
            graph = self._get_graph(workflow_name)
        except KeyError as e:
            yield {"type": "error", "node": "workflow", "content": str(e)}
            return

        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        state = WorkflowState(session_id=session.session_id, question=question)
        merged: dict[str, Any] = state.model_dump()

        try:
            async for update in graph.astream(
                state, self._thread_config(session), stream_mode="updates",
            ):
                for node_name, delta in update.items():
                    if not delta:  # guard nodes returning {} surface as None
                        continue
                    merged.update(delta)
                    if node_name == "gen_sql" and delta.get("sql"):
                        yield {"type": "sql", "node": "gen_sql", "content": delta["sql"]}
                    elif node_name == "execute_sql" and "row_count" in delta:
                        yield {"type": "result", "node": "execute_sql", "row_count": delta["row_count"]}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield {"type": "error", "node": "workflow", "content": str(e)}
            return

        final = WorkflowState.model_validate(merged)
        await self._record_exchange(session, workflow_name, final)

        summary = self._state_summary(final)
        if final.error:
            yield {"type": "error", "node": "output", "content": final.final_response, "summary": summary}
        else:
            yield {"type": "done", "content": final.final_response, "summary": summary}

    # ── Internal helpers ─────────────────────────────────

    async def _record_exchange(
        self,
        session: Session,
        workflow_name: str,
        final: WorkflowState,
    ) -> None:
        """Append the assistant message and persist the session."""
        assistant_msg = Message(
            role="assistant",
            content=final.final_response,
            metadata={
                "trace_id": session.session_id,
                "workflow": workflow_name,
                "sql": final.sql,
                "row_count": final.row_count,
                "verdict": final.verdict,
                "error": final.error,
            },
        )
        session.messages.append(assistant_msg)
        await self._store.save_session(session)

    @staticmethod
    def _state_summary(final: WorkflowState) -> dict[str, Any]:
        """Essentials of the final state for event consumers (e.g. --print)."""
        return {
            "session_id": final.session_id,
            "question": final.question,
            "sql": final.sql,
            "row_count": final.row_count,
            "verdict": final.verdict,
            "reason": final.reason,
            "error": final.error,
            "final_response": final.final_response,
        }

    # ── Token usage ──────────────────────────────────────

    def get_context_usage(self, session: Session) -> dict[str, float]:
        """Estimate token usage for a session."""
        messages = [
            {"role": m.role, "content": m.content}
            for m in session.messages
        ]
        return self._token_counter.estimate_context_usage(messages)

    def should_compact(self, session: Session) -> bool:
        """Check if the session should be compacted."""
        messages = [
            {"role": m.role, "content": m.content}
            for m in session.messages
        ]
        return self._token_counter.should_compact(messages)
