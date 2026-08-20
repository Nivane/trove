"""Observability wiring tests — Langfuse SDK trajectory mode."""

import pytest

from trove.llm import observability


@pytest.fixture
def no_langfuse(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)


@pytest.fixture
def langfuse_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")


class TestEnablement:
    def test_disabled_without_env(self, no_langfuse):
        assert observability.langfuse_enabled() is False
        assert observability.get_client() is None

    def test_enabled_with_env(self, langfuse_env):
        assert observability.langfuse_enabled() is True

    def test_handler_none_when_disabled(self, no_langfuse):
        assert observability.build_callback_handler() is None

    def test_handler_created_when_enabled(self, langfuse_env):
        assert observability.build_callback_handler() is not None

    def test_record_span_noop_when_disabled(self, no_langfuse):
        with observability.record_span("tool.execute_sql", input="SELECT 1") as span:
            assert span is None


class TestConfigureTracing:
    def test_configure_noop_without_llm_callback_registration(self, no_langfuse, monkeypatch):
        """SDK 单通道：不再注册 litellm 回调（避免双记录）。"""
        monkeypatch.setattr("litellm.success_callback", [])
        from trove.llm.tracing import configure_tracing
        from trove.core.config import TracingConfig

        configure_tracing(TracingConfig(enabled=True))
        import litellm
        assert "langfuse" not in litellm.success_callback


class _FakeObservation:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.updated = None

    def update(self, **kwargs):
        self.updated = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeV4Client:
    """只暴露 langfuse v4 的 observation API（无 v2 的 span/generation 方法）。"""

    def __init__(self):
        self.observations = []

    def start_as_current_observation(self, **kwargs):
        obs = _FakeObservation(**kwargs)
        self.observations.append(obs)
        return obs


@pytest.fixture
def fake_v4_client(langfuse_env, monkeypatch):
    client = _FakeV4Client()
    monkeypatch.setattr(observability, "_client", client)
    return client


class TestV4ApiUsage:
    """SDK v4 已移除 start_as_current_span/generation —— 必须走 observation API。"""

    def test_record_span_uses_observation_api(self, fake_v4_client):
        with observability.record_span("tool.execute_sql", input="SELECT 1") as span:
            assert span is not None
            span.update(output={"row_count": 5})
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "span"
        assert obs.kwargs["name"] == "tool.execute_sql"
        assert obs.kwargs["input"] == "SELECT 1"
        assert obs.updated == {"output": {"row_count": 5}}

    def test_gateway_generation_uses_observation_api(self, fake_v4_client):
        from types import SimpleNamespace
        from trove.llm import gateway as gateway_module

        response = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(
                content="SELECT 1", reasoning_content="think",
            ))
        ])
        gateway_module._record_generation(
            "deepseek/deepseek-chat",
            [{"role": "user", "content": "hi"}],
            response,
            {"node": "gen_sql"},
        )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "generation"
        assert obs.kwargs["name"] == "llm"
        assert obs.kwargs["model"] == "deepseek/deepseek-chat"
        assert obs.kwargs["output"] == {"content": "SELECT 1", "reasoning": "think"}
        assert obs.kwargs["metadata"] == {"node": "gen_sql"}

    def test_record_span_preserves_body_exception(self, fake_v4_client):
        """span 内 body 抛异常必须原样穿透——record_span 的 except 不得吞掉
        它并二次 yield(contextlib 会抛 "generator didn't stop after throw()"
        的 RuntimeError 掩盖真实错误)。"""
        import pytest

        with pytest.raises(ValueError, match="business error"):
            with observability.record_span("tool.execute_sql", input="SELECT 1"):
                raise ValueError("business error")

    def test_record_span_passes_metadata(self, fake_v4_client):
        """record_span 的 metadata 必须透传给 observation(会话分组)。"""
        with observability.record_span(
            "cache.hit", input={"question": "q"}, metadata={"session_id": "s1"},
        ):
            pass
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["metadata"] == {"session_id": "s1"}


class TestFailedGeneration:
    """Gap 1: 失败的 LLM 调用必须留失败 generation(level=ERROR)。"""

    async def test_chat_full_failure_records_error_generation(
        self, fake_v4_client, monkeypatch,
    ):
        import litellm
        import pytest
        from trove.llm import gateway as gateway_module

        async def boom(**kwargs):
            raise RuntimeError("API timeout")

        monkeypatch.setattr(litellm, "acompletion", boom)
        gw = gateway_module.LLMGateway(max_retries=1, retry_base_delay=0)
        with pytest.raises(gateway_module.LLMError):
            await gw.chat_full(
                "deepseek/deepseek-chat",
                [{"role": "user", "content": "hi"}],
                metadata={"node": "gen_sql"},
            )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "generation"
        assert obs.kwargs["name"] == "llm"
        assert obs.kwargs["level"] == "ERROR"
        assert obs.kwargs["status_message"] == "API timeout"
        assert obs.kwargs["output"] == {"error": "API timeout"}
        assert obs.kwargs["metadata"] == {"node": "gen_sql"}

    async def test_chat_failure_records_error_generation(
        self, fake_v4_client, monkeypatch,
    ):
        import litellm
        import pytest
        from trove.llm import gateway as gateway_module

        async def boom(**kwargs):
            raise RuntimeError("provider 503")

        monkeypatch.setattr(litellm, "acompletion", boom)
        gw = gateway_module.LLMGateway(max_retries=1, retry_base_delay=0)
        with pytest.raises(gateway_module.LLMError):
            await gw.chat(
                "deepseek/deepseek-chat",
                [{"role": "user", "content": "hi"}],
                metadata={"node": "planner"},
            )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "generation"
        assert obs.kwargs["level"] == "ERROR"
        assert obs.kwargs["status_message"] == "provider 503"
        assert obs.kwargs["metadata"] == {"node": "planner"}


class TestToolSpans:
    """Gap 2: 工具调用记 langfuse span(agent loop observer)。"""

    def test_record_tool_call_span(self, fake_v4_client):
        observability.record_tool_call(
            "probe_query", {"sql": "SELECT 1"}, "OK (3 rows)", None,
        )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "span"
        assert obs.kwargs["name"] == "tool.probe_query"
        assert obs.kwargs["input"] == {"arguments": {"sql": "SELECT 1"}}
        assert obs.kwargs["output"] == {"observation": "OK (3 rows)"}
        assert obs.kwargs["level"] == "DEFAULT"

    def test_record_tool_call_error_level(self, fake_v4_client):
        observability.record_tool_call(
            "probe_query", {"sql": "SELECT 1"}, "", "syntax error",
        )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "span"
        assert obs.kwargs["name"] == "tool.probe_query"
        assert obs.kwargs["level"] == "ERROR"
        assert obs.kwargs["status_message"] == "syntax error"
        assert obs.kwargs["output"] == {"error": "syntax error"}

    def test_record_tool_call_truncates_long_observation(self, fake_v4_client):
        observability.record_tool_call(
            "probe_query", {"sql": "SELECT 1"}, "x" * 5000, None,
        )
        obs = fake_v4_client.observations[0]
        assert len(obs.kwargs["output"]["observation"]) <= 2000

    def test_agent_loop_observer_records_langfuse_tool_span(self, fake_v4_client):
        from trove.llm.agent_loop import _trace_observer

        _trace_observer(
            "check_result", {"sql": "SELECT 1"}, "OK (5 rows)", 12.3, None, "run-1",
        )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "tool.check_result"
        assert obs.kwargs["output"] == {"observation": "OK (5 rows)"}

    def test_agent_loop_observer_records_error_tool_span(self, fake_v4_client):
        from trove.llm.agent_loop import _trace_observer

        _trace_observer(
            "probe_query", {"sql": "SELECT 1"}, "", 5.0, "timeout", "run-1",
        )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "tool.probe_query"
        assert obs.kwargs["level"] == "ERROR"
        assert obs.kwargs["status_message"] == "timeout"


class TestPipelineSpans:
    """Gap 3/4/6: 终态、KB 命中、规则验证的管道级 span。"""

    async def test_output_node_records_result_span(self, fake_v4_client):
        from trove.workflow.nodes.output import output
        from trove.workflow.state import WorkflowState

        state = WorkflowState(
            session_id="s1", question="What is the total?",
            sql="SELECT SUM(amount) FROM loans", verdict="OK", reason="",
            retry_count=2, row_count=3, columns=["SUM(amount)"], rows=[[10]],
            lang="en",
        )
        await output(state)
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "workflow.result"
        assert obs.kwargs["input"]["verdict"] == "OK"
        assert obs.kwargs["input"]["retry_count"] == 2
        assert obs.kwargs["input"]["row_count"] == 3
        assert obs.kwargs["input"]["sql"] == "SELECT SUM(amount) FROM loans"

    async def test_output_node_records_error_path(self, fake_v4_client):
        from trove.workflow.nodes.output import output
        from trove.workflow.state import WorkflowState

        state = WorkflowState(
            session_id="s1", question="q", error="No tables matched", lang="en",
        )
        await output(state)
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "workflow.result"
        assert obs.kwargs["input"]["error"] == "No tables matched"

    async def test_validate_node_records_rules_verify_span(self, fake_v4_client):
        from trove.workflow.nodes.validate import make_validate_rules
        from trove.workflow.state import WorkflowState

        node = make_validate_rules(max_retries=10)
        state = WorkflowState(
            session_id="s1",
            question="Which students are in Alameda county?",
            sql="SELECT name FROM students WHERE county = 'Alameda'",
            columns=["name"], rows=[["Alice"]], row_count=1, lang="en",
        )
        update = await node(state)
        assert update["rules_passed"] is True
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "rules.verify"
        assert obs.kwargs["input"]["question"] == state.question
        assert obs.kwargs["input"]["sql"] == state.sql
        assert obs.updated["output"]["passed"] is True

    async def test_schema_linking_records_kb_hits_span(
        self, fake_v4_client, tmp_path, sqlite_registry,
    ):
        from tests.helpers.kb import ossie_semantics_yaml
        from trove.services.datasource.catalog import CatalogService
        from trove.services.kb.service import KbService
        from trove.workflow.nodes.schema_linking import make_schema_linking
        from trove.workflow.state import WorkflowState

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / sqlite_registry.default_name
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "平均成绩", "aliases": ["平均分"], "mapping": "AVG(students.grade)",
             "tables": ["students"], "definition": "学生平均分"},
        ]), encoding="utf-8")

        node = make_schema_linking(
            catalog=CatalogService(sqlite_registry),
            kb=kb,
            connectors=sqlite_registry,
        )
        update = await node(WorkflowState(
            session_id="s1", question="学生们的平均成绩是多少", lang="zh",
        ))
        assert any(h["term"] == "平均成绩" for h in update["kb_hits"])
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "kb.hits"
        assert obs.kwargs["input"]["question"] == "学生们的平均成绩是多少"

    async def test_fast_match_records_template_hit_span(self, fake_v4_client):
        from trove.services.kb.service import ExampleHit
        from trove.workflow.nodes.fast_match import make_fast_match
        from trove.workflow.state import WorkflowState

        class FakeKB:
            async def ensure_synced(self, **kwargs):
                pass

            async def list_templates(self, datasource):
                return [ExampleHit(
                    question="How many records are in the students table?",
                    sql="SELECT COUNT(*) FROM students",
                    tags=["students", "count", "aggregation"],
                    template=True,
                )]

        class FakeConnectors:
            default_name = "sqlite"

            async def get(self):
                return self

            def dialect(self):
                return "sqlite"

        node = make_fast_match(kb=FakeKB(), connectors=FakeConnectors())
        state = WorkflowState(
            session_id="s1", question="How many students are there?",
            intent="query", matched_tables=["students"], lang="en",
        )
        out = await node(state)
        assert out["fast_path"] is True
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["name"] == "kb.template_hit"
        assert obs.kwargs["input"]["question"] == state.question


class TestCacheHitTrace:
    """Gap 5: 缓存命中不跑图 → 根级独立 trace。"""

    async def test_cache_hit_records_standalone_trace(
        self, session_manager, fake_v4_client,
    ):
        session_manager.config.result_cache = True
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.ask(
            session=session, question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        await session_manager.ask(
            session=session, question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        hits = [o for o in fake_v4_client.observations if o.kwargs.get("name") == "cache.hit"]
        assert hits, "cache.hit trace not recorded on second ask"
        assert hits[0].kwargs["input"]["question"] == "What students are in Alameda county?"
        assert hits[0].kwargs["metadata"]["session_id"] == session.session_id
        summary = hits[0].updated["output"]["summary"]
        assert summary.get("sql") == "SELECT name FROM students;"
