"""Token accounting tests — per-run LLM usage accumulation."""

import pytest

from trove.llm.token_accounting import add, get, pop, reset


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


class TestTokenAccounting:
    def test_add_and_get_accumulate(self):
        add("r1", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        add("r1", {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27})
        assert get("r1") == {"prompt": 30, "completion": 12, "total": 42}

    def test_runs_are_isolated(self):
        add("r1", {"total_tokens": 15})
        add("r2", {"total_tokens": 9})
        assert get("r1")["total"] == 15
        assert get("r2")["total"] == 9

    def test_pop_returns_and_clears(self):
        add("r1", {"prompt_tokens": 1, "total_tokens": 1})
        assert pop("r1") == {"prompt": 1, "completion": 0, "total": 1}
        assert get("r1") is None
        assert pop("r1") is None

    def test_empty_run_id_ignored(self):
        add("", {"total_tokens": 5})
        assert get("") is None

    def test_missing_usage_ignored(self):
        add("r1", {})
        assert get("r1") is None
        add("r1", {"total_tokens": 0})
        assert get("r1") is None

    def test_reset_clears_all(self):
        add("r1", {"total_tokens": 5})
        reset()
        assert get("r1") is None


class TestGatewayWiring:
    """The gateway's local-call recorder feeds the accumulator with usage."""

    def test_record_local_call_accumulates_usage(self):
        from trove.llm.gateway import _record_local_call

        _record_local_call(
            model="test/model",
            messages=[{"role": "user", "content": "hi"}],
            output="ok",
            metadata={"run_id": "r9", "node": "query_sketch"},
            elapsed_ms=10,
            usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        )
        assert get("r9") == {"prompt": 4, "completion": 2, "total": 6}

    def test_record_local_call_without_usage_stays_silent(self):
        from trove.llm.gateway import _record_local_call

        _record_local_call(
            model="test/model",
            messages=[],
            output="ok",
            metadata={"run_id": "r9"},
            elapsed_ms=10,
        )
        assert get("r9") is None

    def test_record_local_call_without_run_id_ignored(self):
        from trove.llm.gateway import _record_local_call

        _record_local_call(
            model="test/model",
            messages=[],
            output="ok",
            metadata={"node": "query_sketch"},
            elapsed_ms=10,
            usage={"total_tokens": 5},
        )
        assert get("anything") is None


class TestRunSummary:
    async def test_done_summary_carries_total_elapsed(self, session_manager):
        """Stats are attached even when token usage is absent (mocked LLM)."""
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        done = None
        async for event in session_manager.ask_stream(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        ):
            if event["type"] == "done":
                done = event
        assert done is not None
        assert done["summary"]["total_elapsed_ms"] >= 0

    async def test_done_summary_includes_token_usage(self, tmp_home, agent_config, sqlite_registry):
        """A gateway reporting usage lands in the done summary as token_usage."""
        import pytest as _pytest
        from trove.agent.session import SessionManager
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.services.datasource.catalog import CatalogService

        class UsageGateway:
            """Scripted responses plus per-call token usage, mirroring the real
            LLMGateway by feeding _record_local_call (which the accumulator reads)."""

            def __init__(self, responses):
                self._responses = iter(responses)
                self._n = 0

            def _record(self, model, messages, out, metadata, **kwargs):
                self._n += 1
                n = self._n
                from trove.llm.gateway import _record_local_call
                _record_local_call(
                    model=model, messages=messages, output=out,
                    metadata=metadata, elapsed_ms=10,
                    usage={"prompt_tokens": n * 10, "completion_tokens": n * 2, "total_tokens": n * 12},
                )

            async def chat(self, model, messages, **kwargs):
                out = next(self._responses)
                self._record(model, messages, out, kwargs.get("metadata"))
                return out

            async def chat_full(self, model, messages, tools=None, **kwargs):
                out = next(self._responses)
                self._record(model, messages, out, kwargs.get("metadata"))
                return {"content": out, "tool_calls": []}

        gateway = UsageGateway(
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"]
        )
        services = GraphServices(
            llm=gateway,
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            config=agent_config,
        )
        graphs = build_graphs(services, multi_candidate=False, query_sketch=False, agentic=False)
        manager = SessionManager(
            config=agent_config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=gateway,
        )
        session = await manager.start_session()

        done = None
        async for event in manager.ask_stream(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        ):
            if event["type"] == "done":
                done = event

        assert done is not None
        usage = done["summary"].get("token_usage")
        assert usage is not None
        assert usage["total"] > 0
        assert usage["prompt"] > 0
        # tally is popped after the run — no leakage into later questions
        from trove.llm.token_accounting import _usage
        assert _usage == {}