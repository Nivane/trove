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
from trove.workflow.state import WorkflowState
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

    **为什么不能用「我没找到消费点」代替**:消费点可以藏在任何一处(路由条件、
    缓存键、降级判定),而它们都不叫 confidence。判据只能是**跑出来的差异**。

    **两个维度,两条用例 —— 一条证不了另一条**:
    - **开关**维度 ⟹ ``test_flag_on_and_off_produce_identical_pipeline_output``;
    - **分数取值**维度 ⟹ ``test_a_low_score_and_a_high_score_deliver_the_same_answer``。

    分数在 ``output`` 里**先算、后读开关**,所以同一道题开/关两跑的分数**本来就是
    同一个值** —— 只读分数的消费点在两跑里**对称**,开关双跑按构造看不见它。要
    证伪「分数的**取值**进了分支」,必须**变分数**,不是变开关。
    """

    #: 两次运行之间**必须逐字段相等**的「链路产物」。
    #: 口径 =「除披露面与时钟外的一切」。三类**故意**不进这一圈,各有各的理由,
    #: 不许默默往里加:
    #: ① ``execution_time_ms`` / ``run_id`` / ``session_id`` —— 两跑天然不同
    #:    (墙钟与会话标识),拿它们做相等断言只会得到一条恒红的用例;
    #: ② ``final_response`` —— 披露面**允许**不同,它由两条用例各自按「差异只许
    #:    出现在披露面上」单独断言(见 ``_without_disclosure``);
    #: ③ ``answer_source`` 是**例外**:它虽然参与披露渲染,但写进 state 是无条件
    #:    的,所以留在这里 —— 两跑必须相等。
    _LINK_ATTRS = (
        "sql", "row_count", "verdict", "answer_source",
        # §11-11 的后半句:急停**只**回退渲染 —— 两个分数**照算照写**,
        # 所以关掉开关依然是同一条 SQL 的**同一个**分数。这三项也进这
        # 一圈,两种故障才分得开:「开关没接上」(两跑全等、披露面也没
        # 变)与「行为被改了」(链路字段跟着变)各有各的红。
        "sql_confidence", "confidence", "confidence_evidence",
        # 分数**取值**一旦进了分支,改变了的东西多半落在这批字段上。它们在
        # 今天的夹具里两跑相等 —— 但在此之前**没有任何断言保证**,是「恰好
        # 没在藏东西」而不是「不许藏」。
        "columns", "chart", "conclusion", "insights", "selection",
        "execution_evidence", "retry_count", "forced", "rules_passed",
        "matched_tables", "error", "complexity", "fast_path", "kb_exact_match",
    )

    #: 两跑之间**天然**不同的渲染行:墙钟。与分数无关,必须排除。
    _CLOCK_LABELS = ("执行耗时", "Execution time")

    @classmethod
    def _without_clock(cls, response: str) -> str:
        return "\n".join(
            ln for ln in response.split("\n")
            if not any(label in ln for label in cls._CLOCK_LABELS)
        )

    @classmethod
    def _without_disclosure(cls, response: str, state) -> str:
        """摘掉**披露面**与墙钟之后剩下的正文。

        「差异只许出现在披露面上」这句话要被断言,就必须先把披露面摘掉再比。
        摘的每一类都有名字,**不许凭手感扩大**:

        - **枚举形态**:``source_line`` 的产出。关态在外、开态在折叠区里各有一份,
          按**渲染器算出来的原文**摘,不靠肉眼找;
        - **分数形态**:``render_line`` 的产出,以及折叠区里那行 ``SQL 置信度``
          (它跟着开关走,见 ``_build_details``)—— 后者与前者不同形,用**字面**
          ``置信度`` 认(与本类两条成员断言的用词一致);
        - **墙钟**:见 ``_without_clock``;
        - **被摘行自己那个空行**:见下。

        **摘一行要连它自己的换行一起摘。** ``render_line`` 与 ``source_line``
        的产出都以一个换行符结尾,接进 ``parts`` 再经换行拼接之后,每个披露行
        都会在自己**后面**留下一个空行。只摘内容行,那个空行就留在原处 ——
        开态的折叠区里有**两行**披露(``SQL 置信度`` 行 + 来源枚举行,紧挨
        着),关态一行都没有,两跑的空白数按构造就差两个,逐字节比对必红。
        **只摘「紧跟在被摘行之后的那个空行」,不是「忽略所有空行」**:别处的
        空行差异仍然留在对比里 —— 「按分数多打一个空行」同样是分数进了渲染,
        正是本类要抓的形态。

        剩下的部分必须**逐字节相同**。少了这一步,一个「开关关掉时顺手把整个
        技术详情折叠区也删了」的改动会全绿 —— 它站在正确的一边(关态确实没有
        分数),出于错误的理由(整段都没了),而设计 §6.2 要防的正是这种
        「半个急停」。
        """
        from trove.agent.answer_source import AnswerSource, source_line

        enum_line = source_line(AnswerSource(state.answer_source), lang="zh").strip()
        kept: list[str] = []
        drop_own_blank = False
        for ln in cls._without_clock(response).split("\n"):
            if drop_own_blank and not ln.strip():
                # 上一个被摘行**自己**的换行;跟着它一起走(见 docstring)。
                drop_own_blank = False
                continue
            drop_own_blank = False
            if (ln.strip() == enum_line
                    or "置信度" in ln or "Confidence" in ln):
                drop_own_blank = True   # 它自己的换行,下一轮一并摘掉
                continue
            kept.append(ln)
        return "\n".join(kept)

    async def test_flag_on_and_off_produce_identical_pipeline_output(self, full_stack):
        from trove.agent.confidence import (
            set_confidence_enabled, reset_confidence_flag,
        )

        async def run(flag: bool) -> WorkflowState:
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
        for attr in self._LINK_ATTRS:
            assert getattr(on, attr) == getattr(off, attr), f"{attr} 被分数改变了"
        assert on.rows == off.rows, "rows 被分数改变了"
        # 反面:两跑**不是**什么都没变 —— 披露面确实换了形态。没有这一条,
        # ``set_confidence_enabled`` 退化成 no-op 时本用例照样全绿(两跑逐字段
        # 相同,因为开关压根没接上),那时它证明的不是 I7,而是「我没接线」与
        # 「行为没变」长得一模一样。有了它:差异**必须存在**,且**只许出现在
        # 披露面上**(上面那一圈比的都是链路产物,必须相等;下面再摘掉披露面与
        # 墙钟比一次,把「只许」两个字落到实处)——
        # 这才是 I7 在**开关这一侧**的完整形态。
        #
        # **断言写成「哪个字在、哪个字不在」,不写成 ``!=``** —— ``!=`` 只证明
        # 「有差异」,不证明**差异是披露面**。它至少漏掉两种故障:①任何一处把
        # 无关变量(时间戳、路径)带进渲染的将来改动,会让它替**真**差异背书,
        # 那时它站在正确的一边却出于错误的理由;②把两个分支**写反**(开态渲染
        # 枚举、关态渲染分数)—— 语义完全颠倒,而差异确实存在,``!=`` 直接放行。
        # 本用例是决策一在**开关这一侧**的证伪点(分数那一侧是下面那条),
        # ``!=`` 撑不起它。
        #
        # 这两个字是 Task 8 已经钉死的形态(两条用例,一正一反):开 ⟹ 披露面是
        # 分数(``置信度`` 在);关 ⟹ **整个响应**里一个字都没有(证据面那条
        # ``SQL 置信度`` 随折叠区一起消失)。
        assert "置信度" in on.final_response, "开关没接上:开态披露面不是分数"
        assert "置信度" not in off.final_response, "开关没接上:关态披露面没回退"
        # 上面两条只说了**披露面换了形态**,没说「其余部分没跟着换」。把披露面
        # 与墙钟摘掉之后,两份答案必须**逐字节相同** —— 这一条才把「**只许**出现
        # 在披露面上」这句话落到实处(见 ``_without_disclosure`` 的 docstring)。
        assert self._without_disclosure(on.final_response, on) == \
            self._without_disclosure(off.final_response, off), \
            "两跑的差异不止披露面(或披露面压根没变)"

    async def test_a_low_score_and_a_high_score_deliver_the_same_answer(
        self, full_stack, monkeypatch,
    ):
        """I7 的另一半:**分数的取值**也不改变行为(开关双跑证伪不了这一半)。

        两个分数在 ``output`` 里先算、后读开关,所以同一道题开/关两跑的分数**本来
        就是同一个值** —— 只读分数的消费点在两跑里对称,开关双跑按构造看不见它。
        这里改的是**分数本身**:把本节点的分数来源换成常数,一跑取地板、一跑取
        天花板,中间跨过任何可能被写进条件分支的阈值。

        **开关两跑都关**:关态交付面不含分数,于是两份答案只需摘掉墙钟行就必须
        **逐字节相同** —— 不做「摘披露行」的裁剪,少一层能掩盖差异的机关。
        """
        # ``import trove.workflow.nodes.output as output_mod`` **不行**:
        # ``nodes/__init__.py`` 里那句 ``from trove.workflow.nodes.output
        # import output`` 把包属性 ``output`` 绑成了**函数**,而
        # ``import a.b as x`` 走的是属性查找 ⟹ 拿到的是函数,
        # ``monkeypatch.setattr(fn, "sql_confidence", ...)`` 当场
        # ``AttributeError``(monkeypatch 先 getattr 存旧值)。
        # ``import_module`` 直接取 ``sys.modules`` 里的模块,不受属性遮蔽影响。
        import importlib
        output_mod = importlib.import_module("trove.workflow.nodes.output")
        from trove.agent.confidence import (
            SCORE_CEIL, SCORE_FLOOR, reset_confidence_flag, set_confidence_enabled,
        )

        async def run(score: float) -> WorkflowState:
            # 替换的是 ``output`` **命名空间里**的那个名字 —— 它是本节点调用时
            # 真正拿到的对象;改 ``trove.agent.confidence.sql_confidence`` 不会
            # 生效(本节点是 ``from ... import`` 进来的)。
            monkeypatch.setattr(
                output_mod, "sql_confidence",
                lambda state, source, **kw: (score, []),
            )
            set_confidence_enabled(False)
            session = await full_stack.start_session(
                project_cwd=f"/tmp/confidence-score-{score}")
            return await full_stack.ask(
                session=session,
                question="哪个地区的平均贷款金额最高？",
                workflow_name="reflection",
            )

        try:
            lo = await run(SCORE_FLOOR)
            hi = await run(SCORE_CEIL)
        finally:
            reset_confidence_flag()

        # **非空性护栏**:补丁真生效了,才谈得上「分数变了而行为没变」。少了这
        # 三条,补丁一旦没打上(名字改了、被挪到别处 import),两跑就是同一个
        # 分数,下面的比对退化成「一次跑跟它自己比」,恒绿。
        assert lo.sql_confidence == SCORE_FLOOR, "补丁没生效:低分那一跑没拿到地板分"
        assert hi.sql_confidence == SCORE_CEIL, "补丁没生效:高分那一跑没拿到天花板分"
        assert lo.confidence < hi.confidence, "补丁没生效:两跑的答案级分数没有分开"

        # 分数进了任何一条分支,都会在这里现形:少一段、多一行、换一个数都算。
        assert self._without_clock(lo.final_response) == self._without_clock(hi.final_response), (
            "分数改变了交付面:同一个问题在低分与高分下交付了不同的答案"
        )
        for attr in self._LINK_ATTRS:
            if attr in ("sql_confidence", "confidence"):
                continue        # 这两项**本来**就该不同 —— 补丁改的就是它们
            assert getattr(lo, attr) == getattr(hi, attr), f"{attr} 被分数改变了"
        assert lo.rows == hi.rows, "rows 被分数改变了"

    async def test_the_score_is_alive_and_carries_evidence(self, full_stack):
        """死指标复活(§11-2)且证据可追(§11-4)。

        §11-2 的另一半是「一批题目上 `avg_confidence` **有方差**」—— 那条要的是
        **批量**,一条问题测不了 stddev,它的落点是评测门禁那个实测指标(见 Step 4),
        不在这里;本用例只管单轮的「非零」。

        **本夹具下 §11-4 那半边是空转的,写明白**:一路顺利 ⟹ 分数**就是**档位基准、
        折损为空,于是 ``if state.sql_confidence != base`` 条件为假、证据循环一次都不
        跑。一个「恒等于基准 + 空证据」的实现能通过本用例全部断言。**这不是漏做** ——
        §5.4 要求的就是「顺路时证据为空」,硬凑一条恒出现的证据反而是要消灭的东西;
        §11-4 的判别力由 Task 5/7 的用例承担,本用例的职责只有「单轮非零」。
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
