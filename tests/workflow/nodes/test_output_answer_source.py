"""output 节点的答案来源披露(设计 §7.3 / 验收 A6):三条路径各一组。

A6 的原话是「三档 ``answer_source`` 与**实际路径**一致」——所以这里不测文案函数
(那是 ``tests/agent/test_answer_source.py`` 的事),测的是「真跑一遍那条路径,输出
的档位是不是那条路径该有的那一档」:

* certified —— 真调 ``fast_match`` 节点命中一个 ``status="certified"`` 的模板;
* compiled  —— 编译器产出权威 SQL 的那一轮(``compiled`` + ``compile_meta``);
* generated —— 其余,含编译 MISS / 软 MISS 骨架 / 修正轮残留的粘滞标记。

``ExampleHit`` 一律本文件现造,不读 ``examples.yml``:治理字段的加载侧是别人的改动面,
依赖它会让这些断言随别人的进度时红时绿。
"""

from __future__ import annotations

import pytest

from trove.services.kb.service import ExampleHit
from trove.workflow.nodes.fast_match import make_fast_match
from trove.workflow.nodes.output import output
from trove.workflow.state import WorkflowState

QUESTION = "How many students are there?"


def make_template(status: str) -> ExampleHit:
    """一条快径模板命中所需的 ``ExampleHit``(治理状态可指)。"""
    return ExampleHit(
        question="How many records are in the students table?",
        sql="SELECT COUNT(*) FROM students",
        tags=["students", "count", "aggregation"],
        template=True,
        status=status,
    )


class FakeKb:
    """fast_match 只用到这两个方法。"""

    def __init__(self, hits: list[ExampleHit]):
        self._hits = hits

    async def ensure_synced(self, **kwargs) -> None:
        pass

    async def list_templates(self, datasource: str) -> list[ExampleHit]:
        return self._hits


class FakeConnectors:
    default_name = "test_db"

    async def get(self, name: str | None = None):
        class _Adapter:
            def dialect(self) -> str:
                return "sqlite"

        return _Adapter()


async def run_fast_match(status: str) -> dict:
    """真跑快径节点,返回它的 partial update。"""
    node = make_fast_match(kb=FakeKb([make_template(status)]), connectors=FakeConnectors())
    return await node(WorkflowState(
        session_id="s1", question=QUESTION, intent="query", matched_tables=["students"],
    ))


def make_state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1",
        "question": QUESTION,
        "sql": "SELECT AVG(grade) FROM students",
        "columns": ["avg_grade"],
        "rows": [[3.5]],
        "row_count": 1,
        "verdict": "OK",
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


# ── certified ────────────────────────────────────────────


class TestCertifiedTier:
    async def test_certified_template_hit_discloses_certified(self):
        state = make_state(**await run_fast_match("certified"))
        update = await output(state)
        assert update["answer_source"] == "certified"
        assert "*来源: 已认证模板" in update["final_response"]

    async def test_draft_template_never_reaches_the_fast_path(self):
        """未认证模板不执行 → 没有命中记录,也就没有任何档位可披露。

        2026-10-02 P1 评测实证快径 4 次命中全部有害,0476 静默错答交付;
        ``fast_match`` 现在只放 ``status == "certified"`` 的模板进快径
        (治理门,见 ``trove/workflow/nodes/fast_match.py``)。本用例钉住
        「跑一遍节点」这一侧的边界:草稿完美匹配也拿不到 SQL,节点只报
        ``{}``。
        """
        assert await run_fast_match("draft") == {}

    async def test_unvetted_template_record_never_discloses_certified(self):
        """一条**没人背书**的模板命中记录 → 不许说「已认证」,但仍是复用。

        CERTIFIED 答的是「有没有人**具名背书**」,没背书就不许用那句话
        (这条断言没变)。REUSED 答的是「这条 SQL **从哪来**」—— 「命中的是
        模板表里的一条」这件事本身与治理字段写没写对无关,所以缺的只是
        「谁背的书」,不是「从哪来」:掉 REUSED,不掉 GENERATED(显示「来源:
        LLM 生成」就是把没进过模型的 SQL 说成模型编的)。

        这里**直接构造状态**而不是跑节点:治理门之后,快径已经不可能再产出
        一条 draft 命中记录(上一条用例钉住了这一点)。但判定函数
        ``for_template_status`` 要对**任何**来源的记录成立 —— 旧 checkpoint
        里存下的历史记录、将来别的写入者 —— 判定点只有一处,不能只在
        「今天的生产者恰好过滤干净」时才对。
        """
        state = make_state(
            fast_path=True,
            sql="SELECT COUNT(*) FROM students",
            kb_hits=[{
                "kind": "template",
                "question": "How many records are in the students table?",
                "sql": "SELECT COUNT(*) FROM students",
                "tags": ["students"],
                "source": "fast_path",
                "status": "draft",
            }],
        )
        update = await output(state)
        assert update["answer_source"] == "reused"
        assert "*来源: 复用知识库资产*" in update["final_response"]
        assert "已认证" not in update["final_response"]
        assert "LLM 生成" not in update["final_response"]

    async def test_leftover_template_hit_after_fallback_does_not_disclose_certified(self):
        """快径命中过、后来回退重生成:kb_hits 里的旧模板命中不算数。

        ``gen_retrieve`` 重生成时会清掉 ``fast_path``(它是权威生成路径),但
        ``kb_hits`` 是累积字段,那条 certified 模板记录还在里面。掉档的依据必须是
        **这一轮**的路径,不是历史里出现过的最好的一条。
        """
        update = await run_fast_match("certified")
        state = make_state(**{**update, "fast_path": False, "compiled": False})
        assert (await output(state))["answer_source"] == "generated"


# ── compiled ─────────────────────────────────────────────


class TestCompiledTier:
    async def test_compiled_run_discloses_compiled(self):
        state = make_state(compiled=True, compile_meta={"outcome": "compiled"})
        update = await output(state)
        assert update["answer_source"] == "compiled"
        assert "*来源: 语义编译*" in update["final_response"]

    async def test_partial_compile_discloses_generated(self):
        """软 MISS 骨架:编译器给了骨架,组件是模型补的 → 不声称编译产物。"""
        state = make_state(
            compiled=True, compile_partial=True, compile_meta={"outcome": "partial"},
        )
        assert (await output(state))["answer_source"] == "generated"

    async def test_stale_compile_flag_discloses_generated(self):
        """``compiled`` 粘滞(重编译 MISS 不回清)→ 最新一轮的结论说了算。"""
        state = make_state(compiled=True, compile_meta={"outcome": "miss"})
        update = await output(state)
        assert update["answer_source"] == "generated"
        assert "语义编译" not in update["final_response"]


# ── generated ────────────────────────────────────────────


class TestGeneratedTier:
    async def test_plain_llm_answer_discloses_generated(self):
        update = await output(make_state())
        assert update["answer_source"] == "generated"
        assert "*来源: LLM 生成 · 建议核对*" in update["final_response"]


# ── 没有答案就没有披露 ───────────────────────────────────


class TestNoDisclosure:
    async def test_no_sql_has_no_disclosure(self):
        """没执行任何查询(空跑):没有答案,不贴来源。"""
        update = await output(make_state(sql="", columns=[], rows=[], row_count=-1))
        assert update["answer_source"] == ""
        assert "来源" not in update["final_response"]

    async def test_error_path_has_no_disclosure(self):
        """披露的是**答案**的来源;错误路径交付的是错误卡片,不是答案。

        output 在错误分支提前返回、不写这个键(见该分支的注释),所以这里断的是
        **state 上**的值:本轮输入已按 ``model_dump()`` 全量重置为空串 —— 空即
        「没有可披露的答案」,不是「这一轮忘了判」。
        """
        update = await output(make_state(error="[ERR:SQL_EXEC] boom"))
        assert update.get("answer_source", "") == ""
        assert "来源" not in update["final_response"]


# ── 位置与语言 ───────────────────────────────────────────


class TestPresentation:
    async def test_source_line_moved_inside_and_score_took_its_place(self):
        """决策三(2026-09-30):披露面给分数,枚举退出。

        本断言的前身是 ``test_source_line_is_outside_the_collapsible_details``
        ——那是**上一个设计的正确断言**(来源行在折叠区外,与数据截止时间同
        位置)。决策三换了披露形态,于是旧的保护对象正是本次要改的东西。
        改它时写明出处,不悄悄翻。
        """
        state = make_state(**await run_fast_match("certified"))
        response = (await output(state))["final_response"]
        outside, _, inside = response.partition("<details>")
        assert "置信度" in outside          # 披露面:分数
        assert "来源" not in outside        # 披露面:没有枚举
        assert "来源" in inside             # 证据面:枚举在这儿

    async def test_english_answer_discloses_in_english(self):
        update = await output(make_state(lang="en"))
        assert "*Source: LLM generated · please verify*" in update["final_response"]


class TestConfidenceDisclosure:
    """设计 2026-09-30 §5.5:披露面给分数。"""

    async def test_score_renders_outside_the_details(self):
        update = await output(make_state(self_check_passed=1))
        outside = update["final_response"].split("<details>")[0]
        assert "置信度" in outside

    async def test_sql_confidence_renders_inside_the_details(self):
        """证据面那条 SQL 置信度:披露面那个数字的「哪个环节弱」。

        它渲染的必须是**这次调用刚算出来的** ``sql_score``,而不是 ``state``
        上的字段 —— 字段的唯一写点就是 output 自己末尾那个返回值,而
        _build_details 在它**之前**跑:从字段读就是读默认值 0.0,这一行会
        永远不出现(写这条时实测过,用例见下)。这是上一条的正面配对:只有
        反面(开关关掉时没有)会漏掉「压根没实现」。
        """
        update = await output(make_state(self_check_passed=1))
        inside = update["final_response"].split("<details>", 1)[1]
        assert "SQL 置信度: 60%" in inside
        assert update["sql_confidence"] == pytest.approx(0.6)

    async def test_no_score_on_the_error_path(self):
        """I4:错误路径不渲染置信度行。"""
        update = await output(make_state(error="[ERR:SQL_EXEC] boom"))
        assert "置信度" not in update["final_response"]
        assert update.get("confidence", 0.0) == 0.0

    async def test_no_score_on_the_metadata_path(self):
        """I4 + §11-6:元数据问答是**另一条早退路径**(``intent_answer`` 那个
        return),与错误路径不共享代码 —— 各钉一条,否则把算分挪到那两个
        return 之上,错误路径那条仍绿。

        这里的 ``intent_answer`` **故意是一句不含「置信度」的普通英文**:
        否则断言会被 payload 自己满足,测的就成了那句文案。
        """
        update = await output(make_state(intent_answer="The dataset covers 2010-2015."))
        assert "置信度" not in update["final_response"]
        assert update.get("confidence", 0.0) == 0.0

    async def test_no_score_on_the_clarification_path(self):
        """I4 + §11-6:澄清反问是**第三条**早退路径(``clarification_question``
        那个 return)。三条路径各一个守卫 —— 它们只共享「不写分数」这件事,
        代码上是三处,一处改了另两处不会跟着红。
        """
        update = await output(make_state(clarification_question="Did you mean A or B?"))
        assert "置信度" not in update["final_response"]
        assert update.get("confidence", 0.0) == 0.0

    async def test_no_score_without_sql(self):
        """I4:空跑没有答案,就没有分数,也不渲染。"""
        update = await output(make_state(sql="", columns=[], rows=[], row_count=-1))
        assert "置信度" not in update["final_response"]
        assert update.get("confidence", 0.0) == 0.0

    async def test_the_old_low_confidence_note_is_gone(self):
        """§5.4-3:旧的「置信度:低(候选 SQL 结果不一致)」**由新的结果
        置信度行取代**,并从折叠区移除 —— 同一件事不两处说。

        ``consensus=False`` 在真链路上**总是**伴随一个 ``selection``
        (select.py 的两条降级 return 都写它),所以这里照那个形状给 ——
        只给 ``consensus=False`` 而不给票率,是不存在的状态,测它没有意义。
        """
        update = await output(make_state(
            consensus=False, selection={"confidence": 1 / 3}))
        assert "置信度:低" not in update["final_response"]
        # 它判的那件事已经被票率折损吸收
        assert update["confidence"] == pytest.approx(0.5 / 3, abs=0.01)

    async def test_switch_off_restores_the_enum_disclosure(self):
        """急停是**完整回退**(§6.2):枚举行回到披露面,而不是什么都不显示。

        半截状态(分数消失、枚举也留在折叠区)既不是旧形态也不是新形态 ——
        是新造出来的第三种,而那让「急停」在事故现场不可用。
        """
        from trove.agent.confidence import set_confidence_enabled, reset_confidence_flag
        set_confidence_enabled(False)
        try:
            update = await output(make_state(**await run_fast_match("certified")))
            outside, _, inside = update["final_response"].partition("<details>")
            assert "来源" in outside
            assert "置信度" not in outside
            # §6.2 右格 + 验收第 11 条:折叠区内**没有** SQL 置信度行。少了这条,
            # 「急停」会停在披露面、把一个新的分数留在折叠区里 —— 那正是 §6.2
            # 点名要避免的「半个功能」。
            assert "SQL 置信度" not in update["final_response"]
            # 枚举行**不许说两遍**:它在披露面(回到今天的形态),折叠区里就
            # 不该再有第二份。
            assert "来源" not in inside
            # 分数**照算照写**:急停不该顺带打瞎 avg_confidence
            assert update["confidence"] > 0
        finally:
            reset_confidence_flag()

    async def test_state_fields_are_written_together(self):
        """三个字段同源同上文 —— 不是各判一次(与 answer_source 同一条纪律)。"""
        update = await output(make_state())
        assert update["confidence"] > 0
        assert update["sql_confidence"] > 0
        assert isinstance(update["confidence_evidence"], list)
