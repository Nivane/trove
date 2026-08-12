"""Session manager — coordinates the full query lifecycle.

This is the high-level orchestrator that:
1. Loads or creates a session
2. Runs the workflow engine
3. Saves results back to the session
4. Handles compaction

It ties together SessionStore, WorkflowEngine, LLMGateway,
and ConnectorRegistry into a single coherent API.
"""

from __future__ import annotations

import asyncio
from typing import Any

from trove.core.types import (
    Message,
    Session,
    WorkflowContext,
    WorkflowResult,
)
from trove.core.config import AgentConfig
from trove.core.errors import SessionError
from trove.core.logging import get_logger
from trove.storage.session_store import SessionStore
from trove.workflow.engine import WorkflowEngine, WorkflowDefinition
from trove.llm.gateway import LLMGateway
from trove.llm.token_counter import TokenCounter

logger = get_logger(__name__)


class SessionManager:
    """High-level orchestration of conversations and queries.

    Usage:
        manager = SessionManager(config, store, engine, llm)
        session = await manager.start_session()
        result = await manager.ask(session, "How many users?")
    """

    def __init__(
        self,
        config: AgentConfig,
        session_store: SessionStore,
        workflow_engine: WorkflowEngine,
        llm_gateway: LLMGateway,
        catalog_service: Any = None,
        connector_registry: Any = None,
    ):
        self.config = config
        self._store = session_store
        self._engine = workflow_engine
        self._llm = llm_gateway
        self._catalog = catalog_service
        self._registry = connector_registry
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

    async def ask(
        self,
        session: Session,
        question: str,
        workflow_name: str = "reflection",
        stream_callback: Any = None,
    ) -> tuple[str, WorkflowResult]:
        """Process a natural language question through the workflow.

        Args:
            session: Current session.
            question: The user's natural language question.
            workflow_name: Which workflow to run ("reflection" or "fixed").
            stream_callback: Optional async callback(Message) for streaming.

        Returns:
            Tuple of (final_response_text, WorkflowResult).
        """
        # Create user message
        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        # Build workflow context
        ctx = WorkflowContext(
            session=session,
            user_message=user_msg,
            config=self.config,
            trace_id=session.session_id,
        )

        # Inject services into config for node access
        self.config._llm_gateway = self._llm  # type: ignore[attr-defined]
        self.config._catalog_service = self._catalog  # type: ignore[attr-defined]
        self.config._connector_registry = self._registry  # type: ignore[attr-defined]

        # Run workflow
        result = await self._engine.run(workflow_name, ctx)

        # Create assistant message
        assistant_msg = Message(
            role="assistant",
            content=result.final_output,
            metadata={
                "trace_id": result.trace_id,
                "workflow": workflow_name,
                "node_count": len(result.nodes),
            },
        )
        session.messages.append(assistant_msg)

        # Persist
        await self._store.save_session(session)

        return result.final_output, result

    async def ask_stream(
        self,
        session: Session,
        question: str,
        workflow_name: str = "reflection",
    ):
        """Stream a query response via async generator.

        Yields events: {"type": "thought"|"sql"|"result"|"done"|"error", "content": ...}

        Args:
            session: Current session.
            question: The user's question.
            workflow_name: Which workflow to run.

        Yields:
            Event dicts for each stage of the response.
        """
        yield {"type": "thought", "content": f"Processing: {question[:80]}..."}

        try:
            response, wf_result = await self.ask(
                session=session,
                question=question,
                workflow_name=workflow_name,
            )

            # Extract and yield SQL if available
            for node in wf_result.nodes:
                if node.node_name == "gen_sql" and "sql" in node.data:
                    yield {"type": "sql", "content": node.data["sql"]}
                elif node.node_name == "execute_sql" and "row_count" in node.data:
                    yield {
                        "type": "result",
                        "content": f"Returned {node.data['row_count']} rows",
                    }

            yield {"type": "done", "content": response}

        except Exception as e:
            yield {"type": "error", "content": str(e)}

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
