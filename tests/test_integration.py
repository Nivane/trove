"""End-to-end integration tests (LangGraph era).

The core MVP closed loop:
  Natural language question → schema_linking → gen_sql → execute_sql
  → reflect → output

All LLM calls are scripted mocks (zero network, zero API keys).
"""

from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.core.types import Message
from trove.services.datasource.catalog import CatalogService
from trove.storage.session_store import SessionStore
from trove.workflow.graphs import GraphServices, build_graphs
from trove.agent.session import SessionManager


class ScriptedLLM:
    """LLM mock that returns scripted responses based on prompt content.

    Maps prompt patterns to canned responses so the full workflow
    can run without any real LLM calls.
    """

    def __init__(self, sql: str, reflect_response: str = "OK", summarize_response: str = "summary"):
        self.sql = sql
        self.reflect_response = reflect_response
        self.summarize_response = summarize_response
        self.call_count = 0

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.call_count += 1
        content = self._content(messages)
        return {"content": content, "tool_calls": []}

    async def chat(self, model: str, messages: list[dict], **kwargs) -> str:
        self.call_count += 1
        return self._content(messages)

    def _content(self, messages):
        # Inspect the last user message to decide which canned response to return
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_content = msg.get("content", "")
                break

        if "Summarize this conversation" in last_content or "请压缩这段对话" in last_content:
            return self.summarize_response
        if "Does this result correctly answer" in last_content:
            return self.reflect_response
        if "failed validation" in last_content or "校验错误" in last_content:
            return f"```sql\n{self.sql}\n```"
        # Default: SQL generation prompt
        return f"```sql\n{self.sql}\n```"


@pytest.fixture
async def full_stack(tmp_path, demo_registry):
    """A fully wired stack with demo data and scripted LLM."""
    store = SessionStore(home_dir=str(tmp_path / "home"))
    catalog = CatalogService(demo_registry)

    config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")

    llm = ScriptedLLM(
        sql="SELECT d.A2 AS district_name, AVG(l.amount) AS avg_loan "
            "FROM loan l "
            "JOIN account a ON l.account_id = a.account_id "
            "JOIN district d ON a.district_id = d.district_id "
            "GROUP BY d.A2 ORDER BY avg_loan DESC",
    )

    # KB 术语解决中文问题匹配（真实使用路径：/kb init + 术语）
    from tests.helpers.kb import ossie_semantics_yaml
    from trove.services.kb.service import KbService
    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / demo_registry.default_name).mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / demo_registry.default_name / "semantics.yml").write_text(
        ossie_semantics_yaml([
            {"term": "平均贷款金额", "aliases": ["平均贷款"],
             "mapping": "AVG(loan.amount)", "tables": ["loan", "account", "district"],
             "definition": "按地区分组的贷款金额均值"},
        ]))

    services = GraphServices(
        llm=llm,
        catalog=catalog,
        connectors=demo_registry,semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
        config=config,
        kb=kb,
    )
    manager = SessionManager(
        config=config,
        session_store=store,
        graphs=build_graphs(services, agentic=False),
        llm_gateway=llm,
    )
    return manager


class TestEndToEnd:
    async def test_full_question_loop(self, full_stack):
        """The complete MVP loop: question → SQL → result → formatted answer."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        state = await full_stack.ask(
            session=session,
            question="哪个地区的平均贷款金额最高？",
            workflow_name="reflection",
        )

        # All pipeline stages produced their artifacts.
        # NOTE: matched_tables is empty for Chinese questions — search_tables
        # tokenizes with ASCII \w only (pre-existing; semantic matching is
        # the v0.2 RAG roadmap).
        assert "loan" in state.sql
        assert "district" in state.sql
        assert state.row_count == 3
        assert state.verdict == "OK"
        assert state.error == ""

        # Execute returned the right answer (Benesov has the highest avg loan)
        assert "Benesov" in str(state.rows)

        # Final output contains the result
        assert "Benesov" in state.final_response
        # 问题/标题不再重复展示
        assert "**问题**" not in state.final_response

        # The exchange was recorded with graph metadata
        assert session.messages[-1].metadata["sql"] == state.sql
        assert session.messages[-1].metadata["verdict"] == "OK"

    async def test_year_grouped_query_generates_chart(self, tmp_path, demo_registry):
        """按年聚合(整数值年份)也必须自动出图——回归:年份列被判为数值度量
        导致无维度 → 无图表。"""
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager

        store = SessionStore(home_dir=str(tmp_path / "home"))
        config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")
        sql = (
            "SELECT strftime('%Y', date) AS year, COUNT(*) AS cnt "
            "FROM loan GROUP BY year ORDER BY year"
        )
        llm = ScriptedLLM(sql=sql)
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(demo_registry),
            connectors=demo_registry,
            semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
            config=config,
        )
        manager = SessionManager(
            config=config,
            session_store=store,
            graphs=build_graphs(services, agentic=False),
            llm_gateway=llm,
        )
        session = await manager.start_session(project_cwd=str(tmp_path))
        state = await manager.ask(
            session=session, question="每年发放的贷款笔数", workflow_name="reflection",
        )
        assert state.error == ""
        assert state.chart is not None, (
            f"year-grouped query produced no chart (columns={state.columns})"
        )
        assert state.chart["type"] in ("line", "bar")
        assert state.chart["dimension"] == "year"
        assert state.chart["measures"] == ["cnt"]

    async def test_multi_turn_conversation(self, full_stack):
        """Multiple questions in the same session accumulate history."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        await full_stack.ask(session=session, question="第一问", workflow_name="reflection")
        await full_stack.ask(session=session, question="第二问", workflow_name="reflection")
        await full_stack.ask(session=session, question="第三问", workflow_name="reflection")

        assert len(session.messages) == 6  # 3 user + 3 assistant

        # History persists across "restarts" (reload from disk)
        loaded = await full_stack.load_session(session.session_id, "/tmp/integration")
        assert len(loaded.messages) == 6
        assert loaded.messages[0].content == "第一问"
        assert loaded.messages[5].content != ""  # last assistant message non-empty

    async def test_fixed_workflow(self, full_stack):
        """The 'fixed' workflow skips reflection."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        state = await full_stack.ask(
            session=session,
            question="List the loans per district",
            workflow_name="fixed",
        )
        assert state.verdict == ""  # reflect never ran
        assert state.row_count == 3

    async def test_session_compaction_flow(self, full_stack):
        """Full compaction flow: many messages → compact → still usable."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        for i in range(5):
            session.messages.append(Message(role="user", content=f"问题{i}"))
            session.messages.append(Message(role="assistant", content=f"回答{i}"))

        # Compact with scripted LLM summarization
        compacted = await full_stack.compact_session(session, keep_recent=1)
        assert len(compacted.messages) == 3
        assert compacted.messages[0].role == "system"

        # Session still usable after compaction
        state = await full_stack.ask(
            session=compacted,
            question="压缩后的新问题",
            workflow_name="reflection",
        )
        assert state.final_response

    async def test_question_with_no_matching_tables(self, full_stack):
        """语义优先(Phase B,决策 4):零命中 = 未覆盖 = 拒绝 + 反问,不生成。"""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        state = await full_stack.ask(
            session=session,
            question="zzz 不存在的表名 zzz",
            workflow_name="reflection",
        )
        assert state.matched_tables == []
        assert state.refusal is not None
        assert state.final_response
        assert "语义模型" in state.final_response


class TestWorkflowEdgeCases:
    async def test_scripted_llm_retry(self, tmp_path, demo_registry):
        """gen_sql subgraph retries when the first response is invalid."""
        store = SessionStore(home_dir=str(tmp_path / "home"))
        catalog = CatalogService(demo_registry)

        config = AgentConfig(home=str(tmp_path / "home"))

        class RetryLLM:
            def __init__(self):
                self.responses = iter([
                    "query",  # route_intent 意图分类
                    "```sql\nSELEC broken sql;\n```",  # 1st: invalid
                    "```sql\nSELECT COUNT(*) FROM client;\n```",  # 2nd: valid
                ])
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                return next(self.responses)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls += 1
                return {"content": next(self.responses), "tool_calls": []}

        llm = RetryLLM()
        manager = SessionManager(
            config=config,
            session_store=store,
            graphs=build_graphs(GraphServices(
                llm=llm,
                catalog=catalog,
                connectors=demo_registry,semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
                config=config,
            ), multi_candidate=False, agentic=False),
            llm_gateway=llm,
        )

        session = await manager.start_session(project_cwd="/tmp/retry")
        state = await manager.ask(
            session=session,
            question="How many client records are there",
            workflow_name="fixed",
        )

        # gen_sql succeeded on the second attempt
        assert llm.calls == 3  # 意图 + 初稿（非法）+ 修正稿
        assert state.sql == "SELECT COUNT(*) FROM client;"
        assert state.error == ""

        # Execution returned the row count
        assert state.row_count == 1


class TestConfidenceDoesNotChangeBehaviour:
    """I7:分数不改变链路行为(设计 §5.4/§11.1)。

    **为什么这条不能用「我没找到消费点」代替**:消费点可以藏在任何一处
    (路由条件、缓存键、降级判定),而它们都不叫 confidence。唯一能证伪的
    判据是同一问题开/关两跑,比对链路产物。
    """

    async def test_flag_on_and_off_produce_identical_pipeline_output(self, full_stack):
        from trove.agent.confidence import (
            set_confidence_enabled, reset_confidence_flag,
        )

        async def run(flag: bool) -> dict:
            set_confidence_enabled(flag)
            session = await full_stack.start_session(
                project_cwd=f"/tmp/confidence-{flag}")
            state = await full_stack.ask(
                session=session,
                question="哪个地区的平均贷款金额最高？",
                workflow_name="reflection",
            )
            return state

        try:
            off = await run(False)
            on = await run(True)
        finally:
            reset_confidence_flag()

        # I7 的「必须相等」那一侧(「必须不同」那一侧是最后那条断言):
        # 开关翻转**只许**改渲染,链路产物逐字段全等。
        for attr in (
            "sql", "row_count", "verdict", "answer_source",
            # §11-11 的后半句:急停**只**回退渲染 —— 两个分数**照算照写**,
            # 所以关掉开关依然是同一条 SQL 的**同一个**分数。这三项也进这
            # 一圈,两种故障才分得开:「开关没接上」(两跑全等、披露面也没
            # 变)与「行为被改了」(链路字段跟着变)各有各的红。
            "sql_confidence", "confidence", "confidence_evidence",
        ):
            assert getattr(on, attr) == getattr(off, attr), f"{attr} 被分数改变了"
        assert on.rows == off.rows, "rows 被分数改变了"
        # 反面:两跑**不是**什么都没变 —— 披露面确实换了形态。没有这一条,
        # ``set_confidence_enabled`` 退化成 no-op 时本用例照样全绿(两跑逐字段
        # 相同,因为开关压根没接上),那时它证明的不是 I7,而是「我没接线」与
        # 「行为没变」长得一模一样。有了它:差异**必须存在**,且**只许出现在
        # ``final_response`` 上**(上面那一圈比的都是链路产物,必须相等)——
        # 这才是 I7 的完整形态。
        #
        # **断言写成「哪个字在、哪个字不在」,不写成 ``!=``** —— ``!=`` 只证明
        # 「有差异」,不证明**差异是披露面**。它至少漏掉两种故障:①任何一处把
        # 无关变量(时间戳、路径)带进渲染的将来改动,会让它替**真**差异背书,
        # 那时它站在正确的一边却出于错误的理由;②把两个分支**写反**(开态渲染
        # 枚举、关态渲染分数)—— 语义完全颠倒,而差异确实存在,``!=`` 直接放行。
        # 本用例是决策一**唯一**的证伪点,``!=`` 撑不起它。
        #
        # 这两个字是 Task 8 已经钉死的形态(两条用例,一正一反):开 ⟹ 披露面是
        # 分数(``置信度`` 在);关 ⟹ **整个响应**里一个字都没有(证据面那条
        # ``SQL 置信度`` 随折叠区一起消失)。
        assert "置信度" in on.final_response, "开关没接上:开态披露面不是分数"
        assert "置信度" not in off.final_response, "开关没接上:关态披露面没回退"

    async def test_the_score_is_alive_and_carries_evidence(self, full_stack):
        """死指标复活(§11-2)且证据可追(§11-4)。

        §11-2 的另一半是「一批题目上 `avg_confidence` **有方差**」—— 那条要的是
        **批量**,一条问题测不了 stddev,它的落点是评测门禁那个实测指标(见 Step 4),
        不在这里;本用例只管单轮的「非零」。
        """
        from trove.agent.answer_source import AnswerSource
        from trove.agent.confidence import TIER_BANDS

        session = await full_stack.start_session(project_cwd="/tmp/confidence-live")
        state = await full_stack.ask(
            session=session,
            question="哪个地区的平均贷款金额最高？",
            workflow_name="reflection",
        )
        assert state.confidence > 0
        assert state.sql_confidence > 0
        assert state.confidence <= state.sql_confidence      # §5.3:不会比 SQL 更可信

        # §11-4(I1 证据可追):分数 ≠ 档位基准 ⟹ 证据非空,且每条三键齐全。
        # 基准直接查档位表 —— 它按构造就是「来源 → 区间」的唯一一张表,
        # 在这里重算一遍基准等于把 Task 5 的判定抄第二遍。
        # 条件句是**必要**的:一路顺利时分数**就是**基准,那时证据为空才正确
        # (设计 §5.4:``certified`` 直出不产生微调项)—— 无条件断言非空会逼着
        # 实现去编一条恒出现的解释,而那条解释正是 §5.5 要消灭的东西。
        # ``answer_source`` 非空由上面两条断言保证(空 ⟹ 分数 0 ⟹ 先红)。
        base = TIER_BANDS[AnswerSource(state.answer_source)][0]
        # 按 ``kind`` 过滤,**不拿整份列表断言**:``confidence_evidence`` 是
        # ``sql_evidence + discounts`` 的并集,用并集断言等于让一条**结果**折损
        # 去替一条**丢失的** SQL 证据背书 —— 断言照样绿,而 §11-4 要抓的
        # 正是那条丢失的。
        sql_items = [e for e in state.confidence_evidence if e["kind"] == "sql"]
        if state.sql_confidence != base:
            assert sql_items, "SQL 分数偏离档位基准,却没有一条 sql 类证据"
        # 反向那条(``confidence`` 低于 ``sql_confidence`` ⟹ 有 result 类证据)
        # **不在这里断言**,推演过:``generated`` 档的 SQL 分数可以触到底
        # (基准 0.50 减满额微调 → ``_clamp`` 拉到 ``SCORE_FLOOR`` 0.05),此时
        # 结果折损乘上去又被同一个 ``_clamp`` 抬回 0.05 —— 两个分数**相等**
        # 而证据确实存在。那条蕴含在一个**可达的**角落里是假的:写成断言,
        # 它会在某个真实用例上红,且红得毫无道理。要兜住它得先排除触底,
        # 复杂度换不回判别力。
        for item in state.confidence_evidence:
            assert item["kind"] in ("sql", "result")
            assert item["name"] and item["why"]
            assert "effect" in item
