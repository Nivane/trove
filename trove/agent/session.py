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
from langgraph.types import Command
import uuid

from trove.core.i18n import L
from trove.prompts import render
from trove.services.sql.format import format_sql
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

DEFAULT_WORKFLOW = "reflection"


def _time_now() -> float:
    import time
    return time.time()


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
        callbacks: list[Any] | None = None,
        kb=None,
        connectors=None,
    ):
        self.config = config
        self._store = session_store
        self._graphs = graphs
        self._llm = llm_gateway
        self._token_counter = TokenCounter()
        self._callbacks = callbacks or []
        self._kb = kb
        self._connectors = connectors
        self._pending_runs: dict[str, dict[str, Any]] = {}  # session_id → pending HITL run info

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

    async def clear_session(self, session: Session) -> Session:
        """Clear all messages and the compaction summary (keeps the session)."""
        return await self._store.clear_session(session)

    async def _maybe_auto_compact(self, session: Session) -> None:
        """Auto-compact before a query when context nears the token limit."""
        try:
            if self.should_compact(session):
                await self.compact_session(session)
        except Exception as e:
            logger.warning("Auto-compaction failed: %s", e)

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

        prompt = render(
            "session/compact",
            lang=self.config.language,
            conversation=conversation,
        )

        try:
            summary = await self._llm.chat(
                model=self.config.target or "openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=16000,
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
        config: dict[str, Any] = {"configurable": {"thread_id": session.session_id}}
        if self._callbacks:
            config["callbacks"] = self._callbacks
        return config

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

        # Auto-compact an over-long session, then build history BEFORE
        # appending the current question
        await self._maybe_auto_compact(session)
        history = self._conversation_history(session)
        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        run_id = str(uuid.uuid4())
        state = WorkflowState(
            session_id=session.session_id,
            question=question,
            run_id=run_id,
            history=history,
            lang=self.config.language,
        )
        self._begin_trace(state)
        self._trace_run_start(state)
        config = dict(self._thread_config(session))
        config["callbacks"] = list(config.get("callbacks") or []) + self._trace_callbacks(run_id)
        result = await graph.ainvoke(state, config)

        if self._is_interrupted(result):
            # HITL 门:图在执行前暂停,返回确认请求。调用方展示后
            # 通过 resume() 用用户的批准/否决继续同一图。
            # 持久化本轮用户消息:resume() 会从 store 重载会话,
            # 未保存的用户输入会丢失(resume 的 _record_exchange 只追加 assistant)。
            await self._store.save_session(session)
            final = WorkflowState.model_validate({k: v for k, v in result.items() if k != "__interrupt__"})
            final = final.model_copy(update={
                "hitl_status": "pending",
                "final_response": self._hitl_confirmation(result, state.lang),
            })
            self._pending_runs[session.session_id] = {
                "run_id": run_id,
                "workflow_name": workflow_name,
            }
            return final

        final = WorkflowState.model_validate(result)

        await self._record_exchange(session, workflow_name, final)
        self._trace_run_finish(run_id, final)
        return final

    @staticmethod
    def _is_interrupted(result: dict[str, Any]) -> bool:
        """True when the graph paused at an interrupt (HITL gate)."""
        return bool(result.get("__interrupt__"))

    @staticmethod
    def _hitl_interrupt(result: dict[str, Any]) -> dict[str, Any] | None:
        """Extract the HITL confirmation payload from an interrupted run."""
        for entry in result.get("__interrupt__", []):
            value = getattr(entry, "value", None)
            if isinstance(value, dict) and value.get("kind") == "confirm_sql":
                return value
        return None

    @classmethod
    def _hitl_confirmation(cls, result: dict[str, Any], lang: str) -> str:
        """Build the confirmation-request text shown to the user on pause."""
        payload = cls._hitl_interrupt(result) or {}
        question = payload.get("question", "")
        sql = payload.get("sql", "")
        semantics = payload.get("semantics", "")
        body = [
            L(lang, "## 执行确认(HITL)\n", "## Execution confirmation (HITL)\n"),
            f"**{L(lang, '问题', 'Question')}**: {question}",
        ]
        if semantics:
            body.append(f"\n**{L(lang, '语义', 'Semantics')}**: {semantics}")
        if sql:
            body.append(f"\n**{L(lang, 'SQL', 'SQL')}**:\n```sql\n{sql}\n```")
        body.append(
            L(
                lang,
                "\n\n该查询尚未执行。回复 y/确认 继续执行,或 n/拒绝 取消。",
                "\n\nThis query has NOT been executed. Reply y/approve to run it, or n/reject to cancel.",
            )
        )
        return "\n".join(body)

    async def resume(
        self,
        session: Session,
        decision: Any,
        workflow_name: str = DEFAULT_WORKFLOW,
    ) -> WorkflowState:
        """Continue a graph paused at the HITL gate with the user's decision.

        Args:
            session: The session whose thread is paused.
            decision: The human decision passed into ``interrupt`` resume
                (e.g. "approve"/"reject", a bool, or a dict).
            workflow_name: Which graph the pending run belongs to.

        Returns:
            The final WorkflowState after resumption.
        """
        graph = self._get_graph(workflow_name)
        pending = self._pending_runs.pop(session.session_id, {})
        run_id = pending.get("run_id", "")
        config = dict(self._thread_config(session))
        if run_id:
            config["callbacks"] = list(config.get("callbacks") or []) + self._trace_callbacks(run_id)

        result = await graph.ainvoke(Command(resume=decision), config)
        final = WorkflowState.model_validate(
            {k: v for k, v in result.items() if k != "__interrupt__"}
        )
        await self._record_exchange(session, workflow_name, final)
        if run_id:
            self._trace_run_finish(run_id, final)
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

        await self._maybe_auto_compact(session)
        history = self._conversation_history(session)
        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        run_id = str(uuid.uuid4())
        state = WorkflowState(
            session_id=session.session_id,
            question=question,
            run_id=run_id,
            history=history,
            lang=self.config.language,
        )
        self._begin_trace(state)
        self._trace_run_start(state)
        merged: dict[str, Any] = state.model_dump()

        try:
            import time as _time
            lang = state.lang
            seq = 0
            last_ts = _time.monotonic()
            run_start = last_ts
            config = dict(self._thread_config(session))
            config["callbacks"] = list(config.get("callbacks") or []) + self._trace_callbacks(run_id)
            async for update in graph.astream(
                state, config, stream_mode="updates",
            ):
                # HITL 中断:图在执行前暂停 —— 发出确认事件并停止本轮流。
                # 调用方展示 SQL+语义后,用 resume() 继续同一线程。
                if "__interrupt__" in update:
                    interrupts = update["__interrupt__"]
                    for entry in interrupts:
                        value = getattr(entry, "value", None)
                        if isinstance(value, dict) and value.get("kind") == "confirm_sql":
                            self._pending_runs[session.session_id] = {
                                "run_id": run_id,
                                "workflow_name": workflow_name,
                            }
                            # 持久化本轮用户消息(resume 重载会话后不丢失)
                            await self._store.save_session(session)
                            merged.update(
                                {k: str(v) for k, v in value.items()}
                            )
                            pending_summary = {
                                "session_id": session.session_id,
                                "question": value.get("question", state.question),
                                "sql": value.get("sql", ""),
                                "semantics": value.get("semantics", ""),
                                "row_count": -1,
                                "verdict": "",
                                "reason": "",
                                "error": "",
                                "kb_hits": [],
                                "insights": [],
                                "hitl_status": "pending",
                                "final_response": "",
                            }
                            yield {
                                "type": "hitl",
                                "node": "hitl",
                                "content": self._hitl_confirmation(
                                    {"__interrupt__": interrupts}, lang,
                                ),
                                "payload": value,
                                "summary": pending_summary,
                            }
                            return
                    continue

                for node_name, delta in update.items():
                    if not delta:  # guard nodes returning {} surface as None
                        continue

                    # 上一节点阶段耗时（更新到达间隔）
                    now = _time.monotonic()
                    elapsed_ms = int((now - last_ts) * 1000)
                    last_ts = now
                    seq += 1

                    # 修正上下文：本次节点执行前挂起的反馈与轮次
                    reason = merged.get("error_feedback", "")
                    retry = merged.get("retry_count", 0)

                    merged.update(delta)

                    # ── Structured step (REPL renders; also in --print) ──
                    yield self._step_event(
                        seq, node_name, delta, elapsed_ms,
                        reason, retry, lang, merged.get("dialect", ""),
                    )

                    # ── Legacy trajectory events (--print compatibility) ──
                    if node_name == "planner" and delta.get("plan"):
                        yield {"type": "plan", "node": "planner", "content": delta["plan"]}
                    if node_name == "reflect" and delta.get("verdict"):
                        yield {
                            "type": "verdict", "node": "reflect",
                            "verdict": delta["verdict"],
                            "reason": delta.get("reason", ""),
                        }
                    if node_name == "select" and "consensus" in delta and not delta["consensus"]:
                        yield {
                            "type": "correction", "node": "select",
                            "content": L(lang, "候选 SQL 结果不一致——本答案置信度低", "Candidate SQLs disagreed — low confidence answer"),
                        }
                    if delta.get("error_feedback"):
                        yield {
                            "type": "correction", "node": node_name,
                            "content": delta["error_feedback"],
                        }

                    if node_name == "gen_sql" and delta.get("sql"):
                        yield {"type": "sql", "node": "gen_sql",
                               "content": format_sql(delta["sql"], merged.get("dialect", ""))}
                    elif node_name == "execute_sql" and "row_count" in delta:
                        yield {"type": "result", "node": "execute_sql", "row_count": delta["row_count"]}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield {"type": "error", "node": "workflow", "content": str(e)}
            return

        final = WorkflowState.model_validate(merged)
        await self._record_exchange(session, workflow_name, final)

        stats = self._run_stats(run_id, run_start)
        self._trace_run_finish(run_id, final, stats)
        summary = self._state_summary(final)
        summary.update(stats)
        if final.error:
            yield {"type": "error", "node": "output", "content": final.final_response, "summary": summary}
        else:
            yield {"type": "done", "content": final.final_response, "summary": summary}

    # ── Internal helpers ─────────────────────────────────

    @staticmethod
    def _step_event(
        seq: int, node_name: str, delta: dict[str, Any],
        elapsed_ms: int, reason: str, retry: int,
        lang: str = "zh", dialect: str = "",
    ) -> dict[str, Any]:
        """Structured trajectory step for REPL rendering / --print."""
        detail: dict[str, Any] = {}

        if node_name == "route_intent":
            detail["intent"] = delta.get("intent", "query")
            detail["llm"] = delta.get("llm")
            detail["intent_evidence"] = delta.get("intent_evidence")
        elif node_name == "schema_linking":
            detail["matched_tables"] = delta.get("matched_tables", [])
            detail["kb_terms"] = sum(
                1 for h in delta.get("kb_hits", []) if h.get("kind") == "term"
            )
        elif node_name == "planner":
            detail["plan"] = delta.get("plan", "")
        elif node_name == "gen_sql":
            detail["sql"] = format_sql(delta.get("sql", ""), dialect)
            detail["attempts"] = delta.get("attempts", 1)
            detail["retry"] = retry
            detail["reason"] = reason
            if delta.get("context_usage"):
                detail["context_usage"] = delta["context_usage"]
        elif node_name == "execute_sql":
            detail["row_count"] = delta.get("row_count", -1)
            detail["execution_time_ms"] = delta.get("execution_time_ms", 0)
            detail["retry"] = retry
            detail["reason"] = reason
        elif node_name == "analyze_error":
            detail["error"] = reason
            detail["analysis"] = delta.get("error_analysis", "")
            detail["rollback"] = delta.get("rollback_target", "")
        elif node_name == "select":
            detail["consensus"] = delta.get("consensus", True)
        elif node_name == "validate":
            detail["reason"] = reason
            detail["retry"] = retry
        elif node_name == "reflect":
            detail["verdict"] = delta.get("verdict", "")
            detail["reason"] = delta.get("reason", "")
        elif node_name == "semantics":
            detail["semantics"] = delta.get("semantics", "")
        elif node_name == "insights":
            detail["insights"] = delta.get("insights", [])
        elif node_name == "hitl":
            detail["hitl_status"] = delta.get("hitl_status", "")
        elif node_name == "output":
            detail["final"] = True

        # LLM call detail (independent of the node chain above)
        if node_name in ("gen_sql", "planner", "reflect", "semantics", "insights") and delta.get("llm"):
            detail["llm"] = delta["llm"]

        return {
            "type": "step",
            "seq": seq,
            "node": node_name,
            "elapsed_ms": elapsed_ms,
            "lang": lang,
            "detail": detail,
        }

    @staticmethod
    def _trace_run_start(state: WorkflowState) -> None:
        """run 事件:活跃 RunTracer 优先(span 树 + run 日志),否则旧扁平事件。"""
        from trove.tracing.runlog import get_tracer
        tracer = get_tracer(state.run_id)
        if tracer is not None:
            tracer.start_run({
                "session_id": state.session_id,
                "question": state.question,
                "lang": state.lang,
            })
            return
        try:
            from trove.tracing.local import add_event
            add_event(state.run_id, {
                "kind": "run",
                "session_id": state.session_id,
                "question": state.question,
                "ts": _time_now(),
            })
        except Exception:
            pass

    @staticmethod
    def _trace_step(state: WorkflowState, step_event: dict[str, Any]) -> None:
        from trove.tracing.runlog import get_tracer
        tracer = get_tracer(state.run_id)
        if tracer is not None:
            tracer.step(step_event)
            return
        try:
            from trove.tracing.local import add_event
            add_event(state.run_id, {"kind": "step", **step_event})
        except Exception:
            pass

    @staticmethod
    def _trace_run_finish(run_id: str, final: WorkflowState, stats: dict[str, Any] | None = None) -> None:
        from trove.tracing.runlog import get_tracer
        summary = SessionManager._state_summary(final)
        if stats:
            summary.update(stats)
        tracer = get_tracer(run_id)
        if tracer is not None:
            tracer.finish(summary)
            return
        try:
            from trove.tracing.local import add_event
            add_event(run_id, {
                "kind": "finish",
                "summary": summary,
            })
        except Exception:
            pass

    @staticmethod
    def _begin_trace(state: WorkflowState):
        """Create the per-run tracer when the local store is configured."""
        from trove.tracing.local import is_configured
        if not is_configured():
            return
        from trove.tracing.runlog import create_tracer
        create_tracer(state.run_id)

    @staticmethod
    def _trace_callbacks(run_id: str) -> list[Any]:
        """LangGraph callback handlers of the active tracer (node spans)."""
        from trove.tracing.runlog import get_tracer
        tracer = get_tracer(run_id)
        return [tracer.callback()] if tracer is not None else []

    async def _capture_lessons(self, final: WorkflowState) -> None:
        """修正闭环成功后，把修正理由沉淀为待确认的经验教训（Hint Bank）。"""
        if self._kb is None or self._connectors is None:
            return
        if final.error or not final.correction_history or not final.sql:
            return
        datasource = self._connectors.default_name or ""
        if not datasource:
            return
        for reason in final.correction_history[-2:]:
            try:
                await self._kb.append_lesson(
                    {
                        "pattern": reason[:120],
                        "note": reason[:200],
                        "sql_snippet": final.sql[:200],
                    },
                    datasource,
                )
            except Exception as e:
                logger.debug("Lesson capture failed: %s", e)

    @staticmethod
    def _conversation_history(session: Session, max_turns: int = 2) -> str:
        """Compact prior exchanges (before the current question) for follow-ups.

        When a compaction summary exists, older turns are replaced by the
        summary and only the most recent turn keeps its verbatim text.
        """
        lines = []
        if session.summary:
            lines.append(f"[summary] {session.summary}")
            recent = session.messages[-2:]  # 最近一轮原文
        else:
            recent = session.messages[-max_turns * 2 :]
        for m in recent:
            if m.content == "":
                continue
            role = "user" if m.role == "user" else "assistant"
            lines.append(f"{role}: {m.content[:300]}")
        return "\n".join(lines)

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
        await self._capture_lessons(final)

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
            "kb_hits": final.kb_hits,
            "semantics": final.semantics,
            "insights": final.insights,
            "hitl_status": final.hitl_status,
            "final_response": final.final_response,
        }

    # ── Token usage ──────────────────────────────────────

    @staticmethod
    def _run_stats(run_id: str, run_start: float) -> dict[str, Any]:
        """Per-question cost stats: total wall time + LLM token usage.

        Token usage comes from the process-level accumulator fed by every
        gateway call carrying this run_id; the tally is popped so results
        never leak across questions in the same process. Wall time is the
        clock from the first graph update to the final summary."""
        import time as _time
        stats: dict[str, Any] = {
            "total_elapsed_ms": int((_time.monotonic() - run_start) * 1000),
        }
        try:
            from trove.llm.token_accounting import pop
            usage = pop(run_id) or {}
        except Exception:
            usage = {}
        if usage:
            stats["token_usage"] = usage
        return stats

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
