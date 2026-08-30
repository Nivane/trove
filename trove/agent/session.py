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
import re
from collections.abc import Callable
from typing import Any, AsyncIterator

from trove.core.types import (
    Message,
    Session,
    Task,
)
from trove.core.config import AgentConfig
from trove.core.errors import SessionError
from trove.core.logging import get_logger
from trove.storage.session_store import SessionStore
from trove.storage.task_store import TaskStore
from trove.llm.gateway import LLMGateway
from trove.llm.token_counter import TokenCounter
from langgraph.types import Command
import uuid
from datetime import datetime, timezone

from trove.agent.tasks import (
    ROWS_PREVIEW,
    cap_cell,
    format_result_packet,
    is_approve_all,
    is_reject,
    looks_likely_multitask,
    looks_multitask,
    looks_task_followup,
    parse_action_json,
    parse_task_json,
)

from trove.core.i18n import L
from trove.prompts import render
from trove.services.sql.format import format_sql
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

DEFAULT_WORKFLOW = "reflection"

# 精确结果缓存:同一会话内、归一化后相同的问句直接返回上次结果(0 LLM)。
# 缓存命中跳过 HITL 确认——首次运行该问题已人工确认过。
RESULT_CACHE_TTL_S = 300.0
CACHEABLE_VERDICTS = {"OK", "EMPTY"}


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
        memory=None,
        role_resolver: Callable[[str], list[str] | None] | None = None,
    ):
        self.config = config
        self._store = session_store
        self._graphs = graphs
        self._llm = llm_gateway
        self._token_counter = TokenCounter()
        self._callbacks = callbacks or []
        self._kb = kb
        self._connectors = connectors
        # 统一记忆 facade(情景记忆/观测回流/偏好提取/画像);None = 记忆关闭
        self._memory = memory
        # 用户角色解析(user_id → roles;None/异常 = 不启用工具 ACL 过滤)
        self._role_resolver = role_resolver
        self._pending_runs: dict[str, dict[str, Any]] = {}  # session_id → pending HITL run info
        self._task_stores: dict[str, TaskStore] = {}  # session_id → TaskStore (惰性,同一会话 .db)
        # 精确结果缓存:key → {"summary", "cached_at"}(进程内存,TTL 惰性淘汰)
        self._result_cache: dict[tuple, dict[str, Any]] = {}

    def _user_tool_roles(self, user_id: str | None) -> list[str] | None:
        """解析用户角色列表(工具 ACL 用);无 resolver/失败 → None = 全可见。"""
        if self._role_resolver is None or not user_id:
            return None
        try:
            return self._role_resolver(user_id)
        except Exception:
            return None

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

    async def list_sessions(
        self,
        project_cwd: str = ".",
        user_id: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List sessions for a project.

        Args:
            project_cwd: Project directory.
            user_id: When given, only sessions owned by this user are
                returned (``None`` = all, the REPL/CLI "local" default).
            offset: Skip the first ``offset`` sessions (by mtime, desc).
            limit: Return at most ``limit`` sessions; ``None`` = no limit.
        """
        return await self._store.list_sessions(
            project_cwd, user_id=user_id, offset=offset, limit=limit
        )

    async def delete_session(self, session_id: str, project_cwd: str = ".") -> bool:
        """Delete a session and its stored data."""
        return await self._store.delete_session(session_id, project_cwd)

    async def rename_session(self, session_id: str, title: str, project_cwd: str = ".") -> bool:
        """Rename a session (empty title falls back to the first question)."""
        return await self._store.set_title(session_id, title, project_cwd)

    async def clear_session(self, session: Session) -> Session:
        """Clear all messages, the compaction summary, and the task list
        (keeps the session; /clear = fresh conversation, fresh tasks)."""
        try:
            await self._task_store(session).clear()
        except Exception as e:
            logger.warning("Task clear failed: %s", e)
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
            compacted = await self._store.compact_session(
                session, summary, keep_recent=keep_recent,
            )
        except Exception as e:
            logger.warning("Compaction failed: %s", e)
            return session

        # 偏好自动提取(机会窗口):压缩 = 对话长上下文的自然边界,趁机让
        # LLM 抽取用户口径/偏好(高置信入 user_facts,低置信落 pending 草稿)。
        # 静默降级——提取失败绝不影响压缩结果。
        await self._extract_preferences_on_compact(session, conversation)
        return compacted

    async def _extract_preferences_on_compact(
        self, session: Session, conversation: str,
    ) -> None:
        """会话压缩后抽取偏好候选(memory.extract_preferences 的薄壳)。"""
        if (
            self._memory is None or self._connectors is None
            or not getattr(self._memory, "enabled", False)
        ):
            return
        try:
            from trove.services.memory.models import MemoryScope

            datasource = (
                (session.metadata or {}).get("datasource", "")
                or self._connectors.default_name
                or ""
            )
            if not datasource:
                return
            await self._memory.extract_preferences(
                MemoryScope(datasource=datasource, user_id=session.user_id or "local"),
                conversation,
                model=self.config.target or "openai/gpt-4o",
                lang=self.config.language,
            )
        except Exception as e:
            logger.debug("Preference extraction on compact failed: %s", e)

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
        datasource: str | None = None,
    ) -> WorkflowState:
        """Process a natural language question through a compiled graph.

        Args:
            session: Current session.
            question: The user's natural language question.
            workflow_name: Which graph to run ("reflection", "fixed", "empty").
            datasource: Datasource name for this request; empty/None keeps
                the current default-datasource behavior.

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
            datasource=datasource or "",
            user_id=session.user_id,
            tool_roles=self._user_tool_roles(session.user_id),
        )
        self._begin_trace(state)
        self._trace_run_start(state)

        # 精确结果缓存:同会话同问句直接返回上次结果(0 LLM)。命中跳过
        # HITL 确认——首次运行该问题已人工确认过。
        cached = self._cache_get(self._cache_key(session, question, datasource))
        if cached is not None:
            self._record_cache_hit(session, run_id, question, cached)
            final = self._cached_final(
                cached, run_id, history, self.config.language,
            )
            await self._record_exchange(session, workflow_name, final)
            self._trace_run_finish(run_id, final)
            return final

        config = self._run_config(session, run_id, state, workflow_name)
        config["callbacks"] = list(config.get("callbacks") or []) + self._trace_callbacks(run_id)
        # 传完整 state dict 而非 pydantic 实例:带 checkpointer 的图上,
        # pydantic 输入的 None 默认值不会覆盖前一轮 checkpoint 残留(如旧
        # refusal),导致同会话下一问被错误短路到 refuse。dict 全量写入所有
        # 通道(含 refusal=None),保证每问都是干净起点。
        result = await graph.ainvoke(state.model_dump(), config)

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
        """Build the confirmation-request text shown to the user on pause.

        Batch sub-tasks (task_context.total > 1) get the three-option
        decision (approve / approve all / stop); single tasks keep the
        legacy y/n wording.
        """
        payload = cls._hitl_interrupt(result) or {}
        question = payload.get("question", "")
        sql = payload.get("sql", "")
        semantics = payload.get("semantics", "")
        task_context = payload.get("task_context") or {}
        body = [
            L(lang, "## 执行确认(HITL)\n", "## Execution confirmation (HITL)\n"),
            f"**{L(lang, '问题', 'Question')}**: {question}",
        ]
        if semantics:
            body.append(f"\n**{L(lang, '语义', 'Semantics')}**: {semantics}")
        if sql:
            body.append(f"\n**{L(lang, 'SQL', 'SQL')}**:\n```sql\n{sql}\n```")
        batch = (task_context.get("total") or 1) > 1
        if batch:
            index = task_context.get("index", 1)
            total = task_context.get("total", 1)
            remaining = task_context.get("remaining", max(total - index, 0))
            body.append(f"\n**{L(lang, '任务', 'Task')}**: {index}/{total}")
            body.append(
                L(
                    lang,
                    "\n\n该查询尚未执行。选择:"
                    f"\n  1. 确认          — 只执行当前任务(剩余 {remaining} 个将逐个确认)"
                    "\n  2. 确认并继续全部 — 当前及剩余任务不再逐一确认"
                    "\n  3. 不继续        — 取消当前任务,剩余任务保持待办",
                    "\n\nThis query has NOT been executed. Choose:"
                    f"\n  1. Approve          — run this task only ({remaining} more will ask individually)"
                    "\n  2. Approve all      — run this and remaining tasks without further confirmation"
                    "\n  3. Stop             — cancel this task, remaining tasks stay pending",
                )
            )
        else:
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
        """Non-streaming HITL resume (compat layer).

        Consumes :meth:`resume_stream` and returns the final state of the
        last completed run. Batch ``approve_all`` continues the remaining
        tasks; the caller only sees the last terminal state.
        """
        content = ""
        summary: dict[str, Any] = {}
        async for ev in self.resume_stream(session, decision, workflow_name):
            if ev.get("type") in ("done", "error"):
                content = ev.get("content", "")
                summary = ev.get("summary") or {}
        return WorkflowState.model_validate({**summary, "final_response": content})

    async def resume_stream(
        self,
        session: Session,
        decision: Any,
        workflow_name: str = DEFAULT_WORKFLOW,
    ) -> AsyncIterator[dict[str, Any]]:
        """HITL resume as an event stream (same shapes as /v1/chat).

        - 单任务:等价于原 resume(),终态以 done/error 事件产出。
        - 批内 approve:只完成被打断的任务,剩余保持 pending
          (回复提示"回复'继续'执行")。
        - 批内 approve_all:完成被打断的任务后,以 auto_approve 继续跑
          剩余任务(不再暂停),全部事件在此流中推送。
        - 批内 reject/不继续:当前任务标记 failed(user_cancelled),
          批终止,剩余保持 pending。
        """
        graph = self._get_graph(workflow_name)
        pending = self._pending_runs.pop(session.session_id, {})
        run_id = pending.get("run_id", "")
        import time as _time
        resume_start = _time.monotonic()
        config = dict(self._thread_config(session))
        if run_id:
            # 复用原 run 的确定性 trace_id:resume 继续同一 trace。
            from trove.workflow.state import WorkflowState
            stub = WorkflowState(
                session_id=session.session_id,
                question="",
                run_id=run_id,
                user_id=session.user_id,
                datasource=pending.get("datasource", ""),
            )
            config = self._run_config(session, run_id, stub, workflow_name)
            config["callbacks"] = list(config.get("callbacks") or []) + self._trace_callbacks(run_id)

        result = await graph.ainvoke(Command(resume=decision), config)
        final = WorkflowState.model_validate(
            {k: v for k, v in result.items() if k != "__interrupt__"}
        )

        # 被打断子任务的消息与状态收尾
        store = self._task_store(session)
        task: Task | None = None
        prefix = ""
        task_id = pending.get("task_id")
        if task_id:
            tasks = await store.load_tasks()
            task = next((t for t in tasks if t.task_id == task_id), None)
            if task is not None:
                total = pending.get("task_total") or len(tasks)
                prefix = L(
                    final.lang,
                    f"**任务 {task.position + 1}/{total}**\n\n",
                    f"**Task {task.position + 1}/{total}**\n\n",
                )

        await self._record_exchange(session, workflow_name, final, task=task, content_prefix=prefix)
        if run_id:
            self._trace_run_finish(run_id, final)

        if task is not None:
            meta = {
                "run_id": run_id,
                "sql": final.sql,
                "row_count": final.row_count,
                "verdict": final.verdict,
                "error": final.error,
            }
            rejected = is_reject(decision)
            if rejected:
                meta["user_cancelled"] = True
            status = "failed" if (rejected or final.error or not final.sql) else "done"
            await store.update_status(task.task_id, status, meta)
            yield {"type": "task", "data": {"tasks": await self._tasks_snapshot(session)}}

        summary = self._state_summary(final)
        summary["hitl_status"] = final.hitl_status
        # resume 段统计:token 是该 run_id 的整条 tally(中断前已累计的也在这里,
        # 中断时只 get 未 pop),_run_stats 一次性 pop 结算。
        summary.update(self._run_stats(run_id, resume_start))
        if final.error:
            yield {"type": "error", "node": "output", "content": final.final_response, "summary": summary}
        else:
            yield {"type": "done", "content": final.final_response, "summary": summary}

        # approve_all → 继续批内剩余 pending 任务(auto_approve,不再暂停)
        if is_approve_all(decision) and pending.get("batch"):
            batch_stats: dict[str, Any] = {}
            self._merge_run_stats(batch_stats, summary)
            tasks = await store.load_tasks()
            remaining = [t for t in tasks if t.status == "pending"]
            if remaining:
                yield {
                    "type": "thought", "node": "start",
                    "content": L(
                        self.config.language,
                        f"已确认全部任务,继续执行剩余 {len(remaining)} 个…",
                        f"All approved — continuing the remaining {len(remaining)} task(s)…",
                    ),
                }
            for t in remaining:
                current = await store.load_tasks()
                target = next((x for x in current if x.task_id == t.task_id), None)
                if target is None:
                    continue
                async for ev in self._run_one_task(
                    session, graph, workflow_name, store, current, target,
                    final.lang, auto_approve=True,
                ):
                    if ev.get("type") in ("done", "error") and ev.get("summary"):
                        self._merge_run_stats(batch_stats, ev["summary"])
                    yield ev
            if remaining:
                # 批收尾:整批结束,前端据此结束本轮(逐任务的 done 只是中间事件);
                # 汇总统计(耗时 + token)随收尾事件带出,前端据此展示;
                # HITL 批场景必然 ≥2 任务,做一次最终综合。
                yield await self._batch_summary_event(
                    session, store, stats=batch_stats or None, synthesize=True,
                )

    async def ask_stream(
        self,
        session: Session,
        question: str,
        workflow_name: str = DEFAULT_WORKFLOW,
        datasource: str | None = None,
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

        # ── 任务层:跨轮推进(解释器)→ 多任务拆解 → 普通单任务 ──
        action = await self._interpret_followup(session, question)
        if action.get("action") != "none":
            async for ev in self._run_task_action(session, graph, question, action, workflow_name, datasource=datasource):
                yield ev
            return

        tasks = await self._decompose_tasks(session, question)
        if tasks:
            async for ev in self._run_task_sequence(session, graph, question, tasks, workflow_name, datasource=datasource):
                yield ev
            return

        # 单任务路径(行为与改造前一致;缓存与事件翻译在 _stream_graph_run 内)
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
            datasource=datasource or "",
            user_id=session.user_id,
            tool_roles=self._user_tool_roles(session.user_id),
        )
        self._begin_trace(state)
        self._trace_run_start(state)

        async for ev in self._stream_graph_run(session, graph, state, workflow_name, run_id):
            yield ev

    async def _stream_graph_run(
        self,
        session: Session,
        graph,
        state: WorkflowState,
        workflow_name: str,
        run_id: str,
        *,
        task: Task | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ONE graph invocation as typed events.

        Shared by the single-task path and the task coordinator (which runs
        sub-tasks in sequence). Behavior matches the legacy inline loop;
        ``task`` only decorates the assistant-message prefix/metadata and the
        HITL pending-run bookkeeping.
        """
        prefix = ""
        if task is not None and state.task_context:
            tc = state.task_context
            prefix = L(
                state.lang,
                f"**任务 {tc['index']}/{tc['total']}**\n\n",
                f"**Task {tc['index']}/{tc['total']}**\n\n",
            )

        # 精确结果缓存:同会话同问句直接产出结果事件(0 LLM),形状与
        # 实跑路径一致(sql → result → done);命中跳过 HITL 确认。
        cached = self._cache_get(self._cache_key(session, state.question, state.datasource))
        if cached is not None:
            import time as _time
            self._record_cache_hit(session, run_id, state.question, cached)
            final = self._cached_final(
                cached, run_id, state.history, state.lang,
            )
            await self._record_exchange(session, workflow_name, final, task=task, content_prefix=prefix)
            stats = self._run_stats(run_id, _time.monotonic())
            self._trace_run_finish(run_id, final, stats)
            summary = dict(cached)
            summary["cached"] = True
            summary.update(stats)
            yield {"type": "sql", "node": "gen_sql",
                   "content": format_sql(final.sql, cached.get("dialect", ""))}
            yield {"type": "result", "node": "execute_sql", "row_count": final.row_count}
            yield {"type": "done", "content": prefix + final.final_response, "summary": summary}
            return

        merged: dict[str, Any] = state.model_dump()

        try:
            import time as _time
            lang = state.lang
            seq = 0
            last_ts = _time.monotonic()
            run_start = last_ts
            config = self._run_config(session, run_id, state, workflow_name)
            # Node-start bridging: a queued LangGraph callback surfaces BEGIN
            # events mid-node so the UI can show *which* node is executing now
            # (not only after it completes) with a live elapsed timer.
            stream_events: asyncio.Queue = asyncio.Queue()
            begin_handler = self._node_start_callback(stream_events)
            config["callbacks"] = (
                list(config.get("callbacks") or [])
                + self._trace_callbacks(run_id)
                + [begin_handler]
            )

            async def _produce() -> None:
                try:
                    # 全量 state dict(见 ask 处注释):pydantic 输入的 None
                    # 默认不覆盖旧 checkpoint 通道,会跨轮残留 refusal。
                    async for update in graph.astream(
                        state.model_dump(), config, stream_mode="updates",
                    ):
                        stream_events.put_nowait(("update", update))
                    stream_events.put_nowait(("end", None))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    stream_events.put_nowait(("error", exc))

            producer = asyncio.ensure_future(_produce())
            try:
                begin_seq = 0
                while True:
                    kind, payload = await stream_events.get()
                    if kind == "begin":
                        begin_seq += 1
                        yield {"type": "begin", "node": payload, "seq": begin_seq}
                        continue
                    if kind == "end":
                        break
                    if kind == "error":
                        raise payload
                    update = payload

                    # HITL 中断:图在执行前暂停 —— 发出确认事件并停止本轮流。
                    # 调用方展示 SQL+语义后,用 resume() 继续同一线程。
                    if "__interrupt__" in update:
                        interrupts = update["__interrupt__"]
                        for entry in interrupts:
                            value = getattr(entry, "value", None)
                            if isinstance(value, dict) and value.get("kind") == "confirm_sql":
                                pending_info: dict[str, Any] = {
                                    "run_id": run_id,
                                    "workflow_name": workflow_name,
                                }
                                if task is not None:
                                    tc = state.task_context or {}
                                    pending_info.update({
                                        "task_id": task.task_id,
                                        "task_index": task.position + 1,
                                        "task_total": tc.get("total", 0),
                                        "batch": True,
                                    })
                                self._pending_runs[session.session_id] = pending_info
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
                                    "conclusion": "",
                                    "hitl_status": "pending",
                                    "final_response": "",
                                }
                                # 消耗统计随中断带出:前端确认气泡即可展示 token 数。
                                # get 不 pop —— 本轮 tally 留给 resume 收尾时一次性结算。
                                pending_summary["total_elapsed_ms"] = int(
                                    round((_time.monotonic() - run_start) * 1000)
                                )
                                try:
                                    from trove.llm.token_accounting import get as _get_usage
                                    usage = _get_usage(run_id) or {}
                                except Exception:
                                    usage = {}
                                if usage:
                                    pending_summary["token_usage"] = usage
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
            finally:
                if not producer.done():
                    producer.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            error_summary = {
                "session_id": session.session_id,
                "question": state.question,
                "sql": merged.get("sql", ""),
                "row_count": merged.get("row_count", -1),
                "verdict": "",
                "reason": str(e),
                "error": str(e),
                "kb_hits": [],
                "insights": [],
                "conclusion": "",
                "hitl_status": "",
                "final_response": "",
            }
            yield {"type": "error", "node": "workflow", "content": str(e), "summary": error_summary}
            return

        final = WorkflowState.model_validate(merged)
        await self._record_exchange(session, workflow_name, final, task=task, content_prefix=prefix)

        stats = self._run_stats(run_id, run_start)
        self._trace_run_finish(run_id, final, stats)
        summary = self._state_summary(final)
        summary.update(stats)
        content = prefix + final.final_response
        if final.error:
            yield {"type": "error", "node": "output", "content": content, "summary": summary}
        else:
            yield {"type": "done", "content": content, "summary": summary}

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
            kh = delta.get("kb_hits", []) if isinstance(delta.get("kb_hits"), list) else []
            detail["kb_terms"] = [
                h.get("term") for h in kh if h.get("kind") == "term"
            ]
            detail["link_detail"] = delta.get("link_detail")
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
        elif node_name == "conclusion":
            detail["conclusion"] = delta.get("conclusion", "")
        elif node_name == "hitl":
            detail["hitl_status"] = delta.get("hitl_status", "")
        elif node_name == "output":
            detail["final"] = True

        # LLM call detail (independent of the node chain above)
        if node_name in ("gen_sql", "planner", "reflect", "semantics", "insights", "conclusion") and delta.get("llm"):
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
        else:
            try:
                from trove.tracing.local import add_event
                add_event(run_id, {
                    "kind": "finish",
                    "summary": summary,
                })
            except Exception:
                pass
        # Langfuse 侧:verdict/timings/tokens 汇总到确定性 trace_id(= run_id)
        try:
            from trove.llm.observability import record_run_finish
            record_run_finish(run_id, summary)
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

    @staticmethod
    def _langfuse_callbacks(run_id: str) -> list[Any]:
        """Per-run Langfuse callback pinned to trace_id = run_id (or []).

        Deterministic trace id lets post-run updates (verdict summary, user
        scores) target the same trace. No-op when Langfuse is not configured.
        """
        from trove.llm.observability import build_callback_handler
        handler = build_callback_handler(trace_id=run_id)
        return [handler] if handler is not None else []

    def _run_config(
        self, session: Session, run_id: str, state: WorkflowState,
        workflow_name: str,
    ) -> dict[str, Any]:
        """Graph run config: thread + callbacks + Langfuse trace-root metadata.

        Root-level langfuse_* keys are read by the SDK CallbackHandler at the
        root chain start, so every trace carries session/user/trace_name/tags
        (feature: trace 根维度). Only the per-run handler is appended here;
        the caller adds runlog tracer callbacks as usual.
        """
        config = dict(self._thread_config(session))
        config["metadata"] = {
            **config.get("metadata", {}),
            "langfuse_session_id": session.session_id,
            "langfuse_user_id": session.user_id or "local",
            "langfuse_trace_name": f"{workflow_name}: {(state.question or '')[:60]}",
            "langfuse_tags": ["trove", workflow_name, state.datasource or ""],
            "run_id": run_id,
            "datasource": state.datasource or "",
        }
        config["callbacks"] = (
            list(config.get("callbacks") or [])
            + self._langfuse_callbacks(run_id)
        )
        return config

    @staticmethod
    def _node_start_callback(stream_events: asyncio.Queue) -> Any:
        """LangGraph callback that pushes a `("begin", node)` item per node start.

        Mirrors the tracer callback's dedup: LangGraph re-fires on_chain_start
        for the same node's chain, and nested subgraph nodes could otherwise
        double-count. Only real nodes are surfaced (`__start__`/`__end__` are
        skipped). The consumer picks these up to emit begin SSE events so the
        UI can render the *currently-executing* node with a live timer.
        """
        from langchain_core.callbacks import BaseCallbackHandler

        class _BeginHandler(BaseCallbackHandler):
            def __init__(self) -> None:
                self._open: dict[str, str] = {}   # run_id -> node
                self._stack: list[str] = []       # 当前嵌套链(栈顶=当前节点)

            async def on_chain_start(
                self, serialized, inputs, *, run_id, parent_run_id=None,
                tags=None, metadata=None, **kwargs,
            ) -> None:
                node = (metadata or {}).get("langgraph_node", "")
                if not node or node in ("__start__", "__end__"):
                    return
                if self._stack and self._stack[-1] == node:
                    return
                self._stack.append(node)
                self._open[run_id] = node
                stream_events.put_nowait(("begin", node))

            async def on_chain_end(self, outputs, *, run_id, **kwargs) -> None:
                node = self._open.pop(run_id, None)
                if node is None:
                    return
                if self._stack and self._stack[-1] == node:
                    self._stack.pop()

        return _BeginHandler()

    async def _capture_lessons(self, final: WorkflowState) -> None:
        """修正闭环成功后，把修正理由沉淀为待确认的经验教训（Hint Bank）。"""
        if self._kb is None or self._connectors is None:
            return
        if final.error or not final.correction_history or not final.sql:
            return
        datasource = final.datasource or self._connectors.default_name or ""
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
    def _conversation_history(session: Session, max_turns: int = 4) -> str:
        """分层历史:摘要 + 最近原文(当前问题之前的上下文)。

        - 有 compaction summary:``[summary] ...`` 打头,再保留最近
          ``max_turns`` 轮原文(分层:早期轮次浓缩为摘要,近期保持逐字)。
        - 无摘要:保留最近 ``max_turns + 2`` 轮原文(窗口比有摘要时更宽,
          因为还没有摘要承载更早的上下文)。
        返回扁平文本,由 context_budget 按相关度/最近度做逐轮裁剪。
        """
        lines = []
        if session.summary:
            lines.append(f"[summary] {session.summary}")
            recent = session.messages[-max_turns * 2 :]
        else:
            recent = session.messages[-(max_turns + 2) * 2 :]
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
        *,
        task: Task | None = None,
        content_prefix: str = "",
    ) -> None:
        """Append the assistant message and persist the session."""
        # 结构化摘要(sql/chart/rows_preview/insights 等)入 metadata:
        # GET /sessions/{id} 透出该字段,前端可原样还原历史 turn。
        metadata: dict[str, Any] = {
            "trace_id": session.session_id,
            "workflow": workflow_name,
            "sql": final.sql,
            "row_count": final.row_count,
            "verdict": final.verdict,
            "error": final.error,
            "summary": self._state_summary(final),
        }
        if task is not None:
            metadata["task_id"] = task.task_id
        assistant_msg = Message(
            role="assistant",
            content=content_prefix + final.final_response,
            metadata=metadata,
        )
        session.messages.append(assistant_msg)
        await self._store.save_session(session)
        # 统一记忆 write-back(情景记忆 + 观测回流 + 失败教训),替代旧的
        # _capture_lessons 单通道;老方法保留供直接调用方(测试)使用。
        await self._observe_memory(final)
        # 结果缓存写钩子(覆盖 ask / resume / ask_stream 三路径)
        self._maybe_cache_exchange(session, final)

    async def _observe_memory(self, final: WorkflowState) -> None:
        """观测回流:情景记忆记录 + 成功→示例草稿 + 修正/失败→pending 教训。

        未配置统一记忆 facade 时回退到旧版 _capture_lessons(修正闭环
        教训沉淀),保持既有行为。全部静默降级:记忆写失败绝不影响回答。
        """
        if self._memory is None or not getattr(self._memory, "enabled", False):
            await self._capture_lessons(final)
            return
        datasource = final.datasource or self._connectors.default_name or ""
        if not datasource:
            return
        from trove.services.memory.models import MemoryScope

        await self._memory.observe(
            scope=MemoryScope(datasource=datasource, user_id=final.user_id or "local"),
            session_id=final.session_id,
            run_id=final.run_id,
            question=final.question,
            sql=final.sql,
            dialect=final.dialect,
            verdict=final.verdict,
            row_count=final.row_count,
            correction_history=final.correction_history,
            matched_tables=final.matched_tables,
            error=final.error,
        )
        # 自动晋升:修正闭环成功后,为该轮修正理由累加置信度(阈值过则自动确认)
        if (
            not final.error and final.correction_history and final.sql
            and getattr(self._memory, "config", None)
            and self._memory.config.promotion_enabled
        ):
            for reason in final.correction_history[-2:]:
                await self._memory.promote_lesson(
                    datasource, reason[:120], evidence_kind="repeated_correction",
                )

    # ── Task coordination ────────────────────────────────

    def _task_store(self, session: Session) -> TaskStore:
        """Per-session TaskStore over the shared SessionStore backend (lazy)."""
        store = self._task_stores.get(session.session_id)
        if store is None:
            # 与 SessionStore 共享同一 StorageBackend(messages/meta/tasks 同库)
            store = TaskStore(
                self._store.backend(),
                session.project_name, session.session_id,
            )
            self._task_stores[session.session_id] = store
        return store

    async def _load_tasks(self, session: Session) -> list[Task]:
        """Task list of the session; storage failure degrades to empty."""
        try:
            return await self._task_store(session).load_tasks()
        except Exception as e:
            logger.warning("Task load failed (%s); treating as no tasks", e)
            return []

    async def _tasks_snapshot(self, session: Session) -> list[dict[str, Any]]:
        """Full task-list snapshot for events / API responses."""
        return [
            {
                "task_id": t.task_id,
                "title": t.title,
                "status": t.status,
                "position": t.position,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
                "metadata": t.metadata,
            }
            for t in await self._load_tasks(session)
        ]

    async def get_tasks(self, session: Session) -> list[dict[str, Any]]:
        """Public API for GET /v1/sessions/{id}/tasks."""
        return await self._tasks_snapshot(session)

    async def _decompose_tasks(self, session: Session, question: str) -> list[Task]:
        """Rule-gated LLM decomposition; [] = single-task path.

        三级门控:
          1. 强化正则命中 → 直接调 LLM 拆解(零行为变化)。
          2. 正则未命中但"疑似多步"(长问句或弱提示词)且
             decompose_llm_judge 开启 → 调同一次 LLM 判断+拆解
             (prompt 输出 {"tasks": [...]},空数组 = 单任务)。
          3. 否则单任务路径,零 LLM 调用(单问题零额外 token)。
        A failed or empty decomposition also degrades to the single-task
        path (the task layer must never become a new failure source).
        """
        if not looks_multitask(question):
            if not (self.config.decompose_llm_judge and looks_likely_multitask(question)):
                return []
        prompt = render(
            "tasks/decompose",
            lang=self.config.language,
            question=question,
        )
        try:
            text = await self._llm.chat(
                model=self.config.target or "openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning("Task decomposition failed (%s); single-task path", e)
            return []
        titles = parse_task_json(text)
        if not titles:
            logger.debug("Decomposition returned no tasks; single-task path")
            return []
        now = datetime.now(timezone.utc)
        return [
            Task(
                task_id=str(uuid.uuid4()),
                session_id=session.session_id,
                title=title,
                status="pending",
                position=position,
                created_at=now,
                updated_at=now,
            )
            for position, title in enumerate(titles)
        ]

    @staticmethod
    def _tasks_block(tasks: list[Task]) -> str:
        """[tasks] 清单块 + [previous results] 结果包,追加到 history,
        注入 gen/planner/意图改写 prompt。

        [previous results] 只带最近一个已完成任务的 ContextPacket
        (done/failed 且落库了 context),供下钻/续问直接引用上一步结论。
        """
        marks = {
            "pending": "[pending]", "in_progress": "[in_progress]",
            "done": "[done]", "failed": "[failed]", "skipped": "[skipped]",
        }
        lines = ["[tasks] 当前任务清单:"]
        for t in tasks:
            lines.append(f"{t.position + 1}. {marks.get(t.status, '[' + t.status + ']')} {t.title}")
        block = "\n".join(lines)
        packet = SessionManager._previous_packet(tasks)
        if packet is not None:
            block += "\n\n" + format_result_packet(packet)
        return block

    @staticmethod
    def _previous_packet(tasks: list[Task]) -> dict | None:
        """最近一个已完成任务的 ContextPacket(位置最大者);无则 None。"""
        completed = [
            t for t in tasks
            if t.status in ("done", "failed") and (t.metadata or {}).get("context")
        ]
        if not completed:
            return None
        return max(completed, key=lambda t: t.position).metadata["context"]

    async def _task_history(self, session: Session, tasks: list[Task]) -> str:
        """会话历史 + [tasks] 块(子任务与跨轮推进共用的 prompt 上下文)。"""
        history = self._conversation_history(session)
        block = self._tasks_block(tasks)
        return f"{history}\n\n{block}" if history and block else (history or block)

    async def _interpret_followup(self, session: Session, question: str) -> dict:
        """跨轮任务操作解释(仅任务列表非空且命中动作提示词时才调 LLM)。

        Returns an action dict from ``parse_action_json``; "none" means the
        message is not a task operation and flows to the normal path.
        """
        tasks = await self._load_tasks(session)
        if not tasks or not looks_task_followup(question):
            return {"action": "none"}
        prompt = render(
            "tasks/interpret",
            lang=self.config.language,
            tasks=[{"status": t.status, "title": t.title} for t in tasks],
            question=question,
        )
        try:
            text = await self._llm.chat(
                model=self.config.target or "openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
            )
        except Exception as e:
            logger.warning("Task follow-up interpretation failed (%s); normal path", e)
            return {"action": "none"}
        return parse_action_json(text)

    async def _run_task_sequence(
        self,
        session: Session,
        graph,
        question: str,
        tasks: list[Task],
        workflow_name: str,
        datasource: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """多任务单轮连跑:逐条执行,事件流中插入 task 快照事件。

        - 新批次替换旧列表(新的多任务指令使旧清单作废)
        - 单条失败不中断整轮:标记 failed 后继续下一条
        - HITL 中断:批暂停,当前任务保持 in_progress,等 resume 决策
        """
        store = self._task_store(session)
        await store.clear()
        for t in tasks:
            await store.save_task(t)

        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        yield {"type": "task", "data": {"tasks": await self._tasks_snapshot(session)}}

        batch_stats: dict[str, Any] = {}
        for t in tasks:
            current = await store.load_tasks()
            target = next((x for x in current if x.task_id == t.task_id), None)
            if target is None:
                continue
            async for ev in self._run_one_task(
                session, graph, workflow_name, store, current, target,
                self.config.language, datasource=datasource,
            ):
                if ev.get("type") in ("done", "error") and ev.get("summary"):
                    self._merge_run_stats(batch_stats, ev["summary"])
                yield ev
            if self._pending_runs.get(session.session_id):
                return  # HITL 批暂停:剩余保持 pending,等 resume 决策

        # 批收尾事件:前端/REPL 据此结束整轮(逐任务的 done 只是中间事件);
        # 汇总统计(耗时 + token)随收尾事件带出;≥2 任务时额外做一次最终综合。
        yield await self._batch_summary_event(
            session, store, stats=batch_stats or None,
            synthesize=len(tasks) >= 2,
        )

    async def _run_one_task(
        self,
        session: Session,
        graph,
        workflow_name: str,
        store: TaskStore,
        tasks: list[Task],
        task: Task,
        lang: str,
        datasource: str | None = None,
        *,
        auto_approve: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """执行单个子任务并流式产出事件(in_progress → 事件流 → 终态标记)。"""
        task.status = "in_progress"
        await store.save_task(task)
        yield {"type": "task", "data": {"tasks": await self._tasks_snapshot(session)}}

        remaining = sum(1 for t in tasks if t.status == "pending")
        total = len(tasks)
        history = await self._task_history(session, tasks)
        run_id = str(uuid.uuid4())
        prev_packet = self._previous_packet(tasks)
        state = WorkflowState(
            session_id=session.session_id,
            question=task.title,
            run_id=run_id,
            history=history,
            lang=lang,
            task_context={"index": task.position + 1, "total": total, "remaining": remaining},
            auto_approve=auto_approve,
            datasource=datasource or "",
            user_id=session.user_id,
            # 步骤间共享:继承上一步 schema linking 锚定的表(schema_linking
            # 节点会与本次新匹配合并,KB 检索与 C1 规则据此锚定)
            matched_tables=list(prev_packet.get("matched_tables") or []) if prev_packet else [],
        )
        self._begin_trace(state)
        self._trace_run_start(state)

        interrupted = False
        terminal: dict[str, Any] | None = None
        async for ev in self._stream_graph_run(session, graph, state, workflow_name, run_id, task=task):
            if ev.get("type") == "hitl":
                interrupted = True
            if ev.get("type") in ("done", "error"):
                terminal = ev
            yield ev

        if interrupted:
            return  # 状态保持 in_progress,resume 决策后再收尾

        summary = (terminal or {}).get("summary") or {}
        status = "done" if (terminal or {}).get("type") == "done" else "failed"
        await store.update_status(task.task_id, status, {
            "run_id": run_id,
            "sql": summary.get("sql"),
            "row_count": summary.get("row_count"),
            "verdict": summary.get("verdict"),
            "error": summary.get("error"),
            # ContextPacket:后续子任务/跨轮通过 [previous results] 与
            # matched_tables 锚点复用本步结论
            "context": {
                "title": task.title,
                "sql": summary.get("sql"),
                "columns": list(summary.get("columns") or []),
                "rows_preview": list(summary.get("rows_preview") or []),
                "row_count": summary.get("row_count"),
                "verdict": summary.get("verdict"),
                "error": summary.get("error"),
                "matched_tables": list(summary.get("matched_tables") or []),
            },
        })
        yield {"type": "task", "data": {"tasks": await self._tasks_snapshot(session)}}

    async def _run_task_action(
        self,
        session: Session,
        graph,
        question: str,
        action: dict,
        workflow_name: str,
        datasource: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """执行跨轮任务操作:continue_next / redo / skip / add。"""
        lang = self.config.language
        store = self._task_store(session)
        tasks = await store.load_tasks()
        action_type = action.get("action")

        if action_type == "add":
            new_tasks = await self._decompose_tasks(session, question)
            if not new_tasks:
                yield {"type": "done", "content": L(
                    lang,
                    "未能从消息中识别出新任务。",
                    "Could not recognize new tasks in this message.",
                )}
                return
            base = len(tasks)
            for t in new_tasks:
                t.position = base + t.position
                await store.save_task(t)
            yield {"type": "task", "data": {"tasks": await self._tasks_snapshot(session)}}
            yield {"type": "done", "content": L(
                lang,
                f"已新增 {len(new_tasks)} 个任务;继续执行请回复\"继续\"。",
                f"Added {len(new_tasks)} task(s). Reply \"continue\" to run them.",
            )}
            return

        if action_type == "skip":
            index = action.get("index", 1) - 1
            if 0 <= index < len(tasks):
                t = tasks[index]
                await store.update_status(t.task_id, "skipped", {"skipped_by": "user"})
                yield {"type": "task", "data": {"tasks": await self._tasks_snapshot(session)}}
                yield {"type": "done", "content": L(
                    lang,
                    f"已跳过任务 {index + 1}: {t.title}",
                    f"Skipped task {index + 1}: {t.title}",
                )}
            else:
                yield {"type": "done", "content": L(lang, "任务序号无效。", "Invalid task index.")}
            return

        # continue_next / redo → 执行目标任务
        if action_type == "redo":
            index = action.get("index", 1) - 1
            target = tasks[index] if 0 <= index < len(tasks) else None
            if target is None:
                yield {"type": "done", "content": L(lang, "任务序号无效。", "Invalid task index.")}
                return
            await store.update_status(target.task_id, "pending", {})
            tasks = await store.load_tasks()
            target = next((t for t in tasks if t.task_id == target.task_id), None)
        else:  # continue_next
            target = next((t for t in tasks if t.status == "pending"), None)
            if target is None:
                yield {"type": "done", "content": L(
                    lang,
                    "没有待办任务。",
                    "No pending tasks.",
                )}
                return

        user_msg = Message(
            role="user",
            content=question,
            metadata={"workflow": workflow_name},
        )
        session.messages.append(user_msg)

        batch_stats: dict[str, Any] = {}
        async for ev in self._run_one_task(
            session, graph, workflow_name, store, tasks, target, lang,
            datasource=datasource,
        ):
            if ev.get("type") in ("done", "error") and ev.get("summary"):
                self._merge_run_stats(batch_stats, ev["summary"])
            yield ev

        if self._pending_runs.get(session.session_id):
            return  # HITL 暂停:等 resume 决策
        yield await self._batch_summary_event(
            session, store, stats=batch_stats or None,
        )

    async def _batch_summary_event(
        self, session: Session, store: TaskStore, stats: dict[str, Any] | None = None,
        synthesize: bool = False,
    ) -> dict[str, Any]:
        """批处理收尾事件:前端据此结束整轮(逐任务的 done 只是中间事件)。

        ``stats`` 汇总各子任务 done 的耗时/token,随收尾事件带出供前端展示。
        ``synthesize`` 为 True 且任务数 ≥2 时,调用一次 fast LLM 把各任务结果
        综合成最终回答,放进 ``summary["final_response"]``(前端置顶展示;
        综合失败/全败时自动回退,逐条答案仍完整可见)。
        """
        snapshot = await self._tasks_snapshot(session)
        done = sum(1 for t in snapshot if t["status"] in ("done", "failed"))
        total = len(snapshot)
        summary: dict[str, Any] = {"batched": True, "tasks": snapshot}
        if stats:
            summary.update(stats)
        if synthesize and total >= 2:
            text = await self._synthesize_batch(snapshot)
            if text:
                summary["final_response"] = text
        return {
            "type": "done",
            "content": L(
                self.config.language,
                f"任务处理完成:{done}/{total} 个任务已结束。",
                f"Task batch finished: {done}/{total} tasks done.",
            ),
            "summary": summary,
        }

    async def _synthesize_batch(self, snapshot: list[dict[str, Any]]) -> str | None:
        """批收尾综合:一次 fast LLM 调用,把各任务结果合成最终回答。

        输入来自快照行的 title/status + 落库 ContextPacket(context):
        失败任务的错误一并带进,让综合能如实说明缺失部分。
        全部任务失败时无结论可综合 → 不调用(零额外 token);
        调用异常或空响应 → None,逐条答案兜底。
        """
        rows: list[dict[str, Any]] = []
        for row in snapshot:
            ctx = (row.get("metadata") or {}).get("context") or {}
            rows.append({
                "status": row.get("status", ""),
                "title": row.get("title", ""),
                "sql": ctx.get("sql"),
                "row_count": ctx.get("row_count"),
                "verdict": ctx.get("verdict"),
                "error": ctx.get("error"),
                "rows_preview": list(ctx.get("rows_preview") or []),
            })
        if not any(r["status"] == "done" for r in rows):
            return None
        try:
            text = await self._llm.chat(
                model=self.config.target or "openai/gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": render(
                        "tasks/synthesize",
                        lang=self.config.language,
                        tasks=rows,
                    ),
                }],
                max_tokens=500,
            )
        except Exception as e:
            logger.warning("Batch synthesis failed (%s); per-task answers fall back", e)
            return None
        return (text or "").strip() or None

    @staticmethod
    def _merge_run_stats(acc: dict[str, Any], summary: dict[str, Any]) -> None:
        """把一次子任务 run 的耗时/token 累加进批汇总(供 batched done 展示)。"""
        ms = summary.get("total_elapsed_ms")
        if ms is not None:
            acc["total_elapsed_ms"] = acc.get("total_elapsed_ms", 0) + int(ms)
        usage = summary.get("token_usage") or {}
        bucket = acc.setdefault("token_usage", {})
        for k, v in usage.items():
            if v:
                bucket[k] = bucket.get(k, 0) + int(v)

    @staticmethod
    def _state_summary(final: WorkflowState) -> dict[str, Any]:
        """Essentials of the final state for event consumers (e.g. --print)."""
        chart_option = None
        if final.chart:
            try:
                from trove.services.viz.echarts import build_echarts_option
                chart_option = build_echarts_option(final.chart)
            except Exception:
                chart_option = None  # 渲染元数据坏时前端仍可退回首选手绘
        rows_preview = [
            [cap_cell(v) for v in row]
            for row in (final.rows or [])[:ROWS_PREVIEW]
        ]
        from trove.services.limits import get_result_limits
        # 全量结果(受查询结果上限约束)随 summary 交付:前端"按查询结果下载"
        # 直接用这份数据,而不去解析答案 markdown 里被截断的展示表格。
        rows_full = [
            [cap_cell(v) for v in row]
            for row in (final.rows or [])[:get_result_limits().max_rows]
        ]
        return {
            "session_id": final.session_id,
            "run_id": final.run_id,
            "question": final.question,
            "sql": final.sql,
            "row_count": final.row_count,
            "verdict": final.verdict,
            "reason": final.reason,
            "error": final.error,
            "kb_hits": final.kb_hits,
            "semantics": final.semantics,
            "insights": final.insights,
            "conclusion": final.conclusion,
            "hitl_status": final.hitl_status,
            "final_response": final.final_response,
            "columns": list(final.columns),
            "rows": rows_full,
            "rows_preview": rows_preview,
            "matched_tables": list(final.matched_tables),
            "chart": final.chart,
            "chart_option": chart_option,
        }

    # ── Result cache (exact-question, in-process) ────────

    @staticmethod
    def _normalize_question(q: str) -> str:
        """归一化问句:小写 + 非单词字符置空格(\\W 保留 CJK 等 Unicode 字母)
        + 折叠空白。同问的标点/大小写变体命中同一个键。"""
        text = (q or "").lower()
        text = re.sub(r"\W", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _cache_enabled(self) -> bool:
        return bool(self.config.result_cache)

    def _cache_key(self, session: Session, question: str, datasource: str | None = None) -> tuple:
        """键 = (会话, 数据源, 归一化问句)。数据源隔离:同一问句在不同
        库上是不同问题。"""
        ds = datasource or ""
        if not ds and self._connectors is not None:
            try:
                ds = self._connectors.default_name or ""
            except Exception:
                ds = ""
        return (session.session_id, ds, self._normalize_question(question))

    def _cache_get(self, key: tuple) -> dict[str, Any] | None:
        hit = self._result_cache.get(key)
        if hit is None:
            return None
        if _time_now() - hit["cached_at"] > RESULT_CACHE_TTL_S:
            self._result_cache.pop(key, None)  # 惰性淘汰
            return None
        return hit["summary"]

    def _cache_put(self, key: tuple, summary: dict[str, Any]) -> None:
        self._result_cache[key] = {"summary": summary, "cached_at": _time_now()}

    @staticmethod
    def _record_cache_hit(
        session: Session, run_id: str, question: str, cached: dict[str, Any],
    ) -> None:
        """缓存命中独立 trace:图未执行,根级开一条 cache.hit(无 Langfuse 时 no-op)。

        本地 runlog 已有 finish 事件;这里补 langfuse 侧"零 LLM 直接返回"
        的可视性,output 带上次已验证的 summary(sql/verdict/行数/错误)。
        """
        from trove.llm.observability import langfuse_trace_id, record_span

        with record_span(
            "cache.hit",
            input={"question": question},
            metadata={"session_id": session.session_id, "run_id": run_id},
            trace_context={"trace_id": langfuse_trace_id(run_id)},
        ) as span:
            if span is not None:
                span.update(output={"summary": cached})

    def _maybe_cache_exchange(self, session: Session, final: WorkflowState) -> None:
        """结果缓存写门:启用 ∧ 无错误/反馈 ∧ 裁决可缓存 ∧ SQL 非空。

        错误/RETRY/NO_SQL 不缓存(下次同问需要重新跑);OK/EMPTY 且真
        实执行过(SQL 非空、row_count ≥ 0)才写。
        """
        if not self._cache_enabled():
            return
        if final.error or final.error_feedback:
            return
        if final.verdict not in CACHEABLE_VERDICTS:
            return
        if not (final.sql or "").strip() or final.row_count < 0:
            return
        summary = self._state_summary(final)
        summary["cached"] = True
        summary["dialect"] = final.dialect
        self._cache_put(self._cache_key(session, final.question, final.datasource), summary)

    def _cached_final(
        self, cached: dict[str, Any], run_id: str, history: str, lang: str,
    ) -> WorkflowState:
        """缓存命中 → 重建 WorkflowState(只取模型字段;run_id/history/lang 用本轮值)。"""
        summary = {k: v for k, v in cached.items() if k in WorkflowState.model_fields}
        final = WorkflowState.model_validate(summary)
        return final.model_copy(update={
            "run_id": run_id,
            "history": history,
            "lang": lang,
        })

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
