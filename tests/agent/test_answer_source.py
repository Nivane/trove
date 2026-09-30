"""答案来源三档(设计 §6.4 / §7.3 / I6):判定、文案、SSE 提取。

**本文件的 fixture 全部自带**(``ExampleHit`` / ``WorkflowState`` 现造)。治理字段的
**加载侧**(YAML 的 ``governance:`` 块 → ``ExampleHit.status``)是另一处改动面,不归这里
管 —— 依赖它会让本文件随别人的进度时红时绿,而这里要钉的是「拿到状态之后怎么判」。

三组断言对应三件不同的事:

* ``TestTemplateTier`` / ``TestCompileTier`` —— 判定规则本身(I6:判定不出→generated);
* ``TestSourceLine`` —— 文案(§7.3 的三句 + R5 不说「准确」);
* ``TestSseDisclosure`` —— 披露通道(事件顶层那个字段怎么来的)。
"""

from __future__ import annotations

import pytest

from trove.api.sse import with_answer_source
from trove.agent.answer_source import (
    AnswerSource,
    for_compile,
    for_template_status,
    resolve,
    source_line,
)
from trove.workflow.state import WorkflowState


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "How many students are there?"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def template_hit(status: str) -> dict:
    """fast_match 命中后写进 ``kb_hits`` 的那一条(形状由该节点保证)。"""
    return {
        "kind": "template",
        "question": "How many records are in the students table?",
        "sql": "SELECT COUNT(*) FROM students",
        "tags": ["students", "count"],
        "source": "fast_path",
        "status": status,
    }


# ── 档位取值 ─────────────────────────────────────────────


class TestTiers:
    def test_wire_values_are_the_frozen_contract(self):
        """四个字符串是要上 wire 的契约(答案渲染 / SSE / 台账落账同一份)。

        按**可信度降序**排列:CERTIFIED(有人认证)> REUSED(人确认过的资产,
        但没人具名背书)> COMPILED(机器确定性推导)> GENERATED(模型生成)。
        §8.4 的判据是「用户关心这条 SQL 有没有被人验证过」,所以 REUSED 在
        COMPILED 之上 —— 前者有人经手,后者只是算出来的。
        """
        assert [s.value for s in AnswerSource] == [
            "certified", "reused", "compiled", "generated",
        ]


# ── 快径:模板治理状态 → 档位 ────────────────────────────


class TestTemplateTier:
    """只有 ``certified`` 升档。

    模板是**确定性产物**这件事不足以升档:CERTIFIED 这一档承诺的是「有人验证过」,
    不是「算出来的」—— 这两个意思分开,是这个设计存在的理由(§8.4)。
    """

    def test_certified_template_is_certified(self):
        assert for_template_status("certified") is AnswerSource.CERTIFIED

    @pytest.mark.parametrize(
        "status", ["draft", "deprecated", "", "unknown", "CERTIFIED", " certified "],
    )
    def test_everything_else_is_reuse_not_generation(self, status):
        """认不出的状态掉到 REUSED,**不掉到 GENERATED**。

        这两档答的不是同一个问题:CERTIFIED 答「有没有人具名背书」,REUSED 答
        「这条 SQL 从哪来」。状态坏掉只影响**前者** —— 元数据坏了确实没有资格
        声称有人认证;但「这条 SQL 是从模板表里取的」是模板命中这件事本身保证的,
        与治理字段写没写对无关。

        原来的实现把两者压成一档,于是坏状态 + 模板命中会显示「来源: LLM 生成」:
        一条根本没进过模型的 SQL 被说成模型编的。披露装置说假话比不披露更坏。
        """
        assert for_template_status(status) is AnswerSource.REUSED


# ── 编译通道 → 档位 ──────────────────────────────────────


class TestCompileTier:
    def test_clean_compile_is_compiled(self):
        assert for_compile(
            compiled=True, partial=False, latest_outcome="compiled",
        ) is AnswerSource.COMPILED

    def test_no_compiled_sql_is_generated(self):
        """没编译出权威 SQL(包括编译 MISS 后模型自己写)= 生成。"""
        assert for_compile(
            compiled=False, partial=False, latest_outcome="miss",
        ) is AnswerSource.GENERATED

    def test_partial_skeleton_is_generated(self):
        """软 MISS 骨架:权威结构之外还有模型补的组件 → 不声称「编译器给的」。"""
        assert for_compile(
            compiled=True, partial=True, latest_outcome="partial",
        ) is AnswerSource.GENERATED

    def test_stale_flag_is_not_trusted(self):
        """``compiled`` 是**粘滞位**,最新一轮编译 MISS 才是真话。

        query_sketch 只在自己的编译成功时置 True,重跑 MISS 时不回清 —— 于是
        「先编译成功、后来一路回退重编译失败、最后答案由模型写出」的运行里,
        只看 ``compiled`` 就会把模型写的 SQL 标成编译器产物。
        """
        assert for_compile(
            compiled=True, partial=False, latest_outcome="miss",
        ) is AnswerSource.GENERATED

    def test_missing_latest_outcome_is_not_evidence(self):
        """拿不到最新结论(旧 checkpoint / 没写)= 判定不出 → 保守(I6)。"""
        assert for_compile(
            compiled=True, partial=False, latest_outcome="",
        ) is AnswerSource.GENERATED


# ── 判定入口 ─────────────────────────────────────────────


class TestResolve:
    def test_fast_path_beats_compile(self):
        """两条路径语法互斥(§8.5);真同时成立时以快径为准(它才是 SQL 的来源)。"""
        state = make_state(
            fast_path=True,
            kb_hits=[template_hit("certified")],
            compiled=True,
            compile_meta={"outcome": "compiled"},
        )
        assert resolve(state) is AnswerSource.CERTIFIED

    def test_fast_path_without_a_template_record_is_still_reuse(self):
        """命中标记在、命中详情不在(状态被裁剪/旧 checkpoint)→ 仍是复用,只是不认证。

        ``fast_path`` 这个标记本身就是「这条 SQL 由快径产出」的证据,它不依赖
        ``kb_hits`` 还在不在。丢掉的是模板的**治理状态**,不是**来源** ——
        所以掉到 REUSED,不掉到 GENERATED。
        """
        state = make_state(fast_path=True, kb_hits=[], sql="SELECT COUNT(*) FROM students")
        assert resolve(state) is AnswerSource.REUSED

    def test_template_record_without_status_is_still_reuse(self):
        """治理字段是后加的:旧记录没有 ``status`` 键 → 等同于 draft(I1)。

        同上:字段缺席只说明「不知道有没有人认证」,不说明「这条 SQL 是模型写的」。
        """
        hit = template_hit("certified")
        del hit["status"]
        state = make_state(fast_path=True, kb_hits=[hit])
        assert resolve(state) is AnswerSource.REUSED

    def test_ordinary_llm_answer_is_generated(self):
        state = make_state(sql="SELECT 1", compiled=False)
        assert resolve(state) is AnswerSource.GENERATED

    def test_term_hits_do_not_masquerade_as_a_fast_path(self):
        """``kb_hits`` 里其它 kind(术语/示例)与快径无关,不得被当成模板命中。"""
        state = make_state(
            kb_hits=[{"kind": "term", "term": "平均成绩", "status": "certified"}],
            sql="SELECT AVG(grade) FROM students",
        )
        assert resolve(state) is AnswerSource.GENERATED


# ── KB 精确命中:复用的第二条路 ───────────────────────────


class TestExactMatchTier:
    """``kb_exact_match`` 直接取示例 SQL、**跳过模型生成**
    (``graphs.py`` 里 ``if kb_exact_match is not None:`` → ``update["sql"] = kb_exact_match["sql"]``)。

    它与快径是两条不同的复用路(模板 vs 示例),但披露档位同属复用:两者都
    「没经过模型」,而这正是用户要区分的那件事。
    """

    def test_exact_kb_match_discloses_reused_not_generated(self):
        state = make_state(kb_exact_match=True)
        assert resolve(state) is AnswerSource.REUSED

    def test_exact_match_beats_a_compile_flag_from_earlier_in_the_run(self):
        """精确命中跳过生成,但编译位可能在**这一轮之前**就置上了。

        停在 COMPILED 会把「逐字复用的示例 SQL」说成「编译器产物」—— 两者都没
        经过模型,可来源不同,而披露的全部内容就是来源。
        """
        state = make_state(
            kb_exact_match=True,
            compiled=True,
            compile_meta={"outcome": "compiled"},
        )
        assert resolve(state) is AnswerSource.REUSED

    def test_an_ordinary_run_is_not_touched_by_the_new_tier(self):
        """新增一档不得把普通生成路径也吸进去(否则 I6 反向失效)。"""
        state = make_state(sql="SELECT 1", kb_exact_match=False, compiled=False)
        assert resolve(state) is AnswerSource.GENERATED


# ── 文案(§7.3 / R5) ─────────────────────────────────────


class TestSourceLine:
    def test_certified_copy(self):
        assert source_line(AnswerSource.CERTIFIED) == "*来源: 已认证模板*\n"

    def test_compiled_copy(self):
        assert source_line(AnswerSource.COMPILED) == "*来源: 语义编译*\n"

    def test_compiled_copy_does_not_claim_the_asset_library_is_empty(self):
        """「未命中资产库」是**事实断言**,而它可以是假的。

        走了编译路径 ≠ 示例检索一定为空:检索可能命中了、只是这一轮没采用。
        §7.3 的原文带这半句,实现照抄了 —— 于是披露装置在一个它无从得知的
        事实上说了话。披露行只该说机制能保证的部分。
        """
        assert "未命中" not in source_line(AnswerSource.COMPILED)
        assert "no asset match" not in source_line(AnswerSource.COMPILED, lang="en")

    def test_reused_copy_names_the_reuse_not_the_model(self):
        """复用这一档必须让用户看见「复用」二字。

        这是这套能力的标题:资产被复用了几次、复用的是哪一条 —— 统计段要说的
        就是这件事,文案把来源说成「LLM 生成」,统计段就没有落脚点。
        """
        line = source_line(AnswerSource.REUSED)
        assert "复用" in line
        assert "LLM" not in line

    def test_generated_copy(self):
        assert source_line(AnswerSource.GENERATED) == "*来源: LLM 生成 · 建议核对*\n"

    def test_certified_prints_the_ledger_stats_when_it_has_them(self):
        """有台账统计就写全(§7.3 那一行的样子)。"""
        line = source_line(AnswerSource.CERTIFIED, runs=47, p50_ms=320)
        assert line == "*来源: 已认证模板 · 命中 47 次 · P50 320ms*\n"

    def test_certified_without_p50_samples_still_reports_runs(self):
        """**三态之一**:跑过、但没有耗时样本(样本不足/台账没算)。

        不能因为 P50 缺席就把整行统计吞掉 —— 那会把「命中 47 次」也一起丢掉;
        也不能把 P50 写成 0 —— 那是在讲「快到 0ms」。
        """
        line = source_line(AnswerSource.CERTIFIED, runs=47)
        assert line == "*来源: 已认证模板 · 命中 47 次*\n"
        assert "P50" not in line

    def test_certified_without_ledger_reports_no_stats(self):
        """**三态之二**:台账拿不到 → 一个字都不写。

        「没有统计」与「统计是 0」不是一回事:前者是没查,后者是查过、从没被用过。
        """
        assert source_line(AnswerSource.CERTIFIED, runs=None, p50_ms=None) == (
            "*来源: 已认证模板*\n"
        )

    def test_english_copy(self):
        assert source_line(AnswerSource.COMPILED, lang="en") == (
            "*Source: semantic compile*\n"
        )
        assert source_line(AnswerSource.REUSED, lang="en") == (
            "*Source: reused knowledge-base asset*\n"
        )
        assert source_line(
            AnswerSource.CERTIFIED, lang="en", runs=47, p50_ms=320,
        ) == "*Source: certified template · 47 runs · P50 320ms*\n"

    @pytest.mark.parametrize("source", list(AnswerSource))
    def test_never_claims_accuracy(self, source):
        """R5:三档说的是「谁给的」,不是「对不对」。文案里不许出现「准确」。"""
        assert "准确" not in source_line(source, runs=1, p50_ms=1)
        assert "accurate" not in source_line(source, lang="en", runs=1, p50_ms=1)


# ── SSE 披露(设计 §7.3:事件带 answer_source) ─────────────


class TestSseDisclosure:
    """终态事件顶层带档位。测试放在这里是因为本任务只开两个测试文件,
    而 ``with_answer_source`` 是 ``trove/api/sse.py`` 里唯一的公开逻辑。
    """

    def test_stamps_the_terminal_summary_value(self):
        event = {"content": "答案", "summary": {"answer_source": "certified"}}
        assert with_answer_source(event)["answer_source"] == "certified"

    def test_events_without_a_summary_are_left_alone(self):
        """step/thought/task 事件没有答案可披露 —— 不加字段,也不编一个。"""
        assert with_answer_source({"content": "处理中"}) == {"content": "处理中"}

    def test_answers_without_a_tier_get_no_field(self):
        """元数据问答/澄清反问的总结里是空串:那类回答不走这条披露,不加字段。"""
        assert "answer_source" not in with_answer_source({"summary": {"answer_source": ""}})

    def test_does_not_mutate_the_caller_event(self):
        """传输层不改上游数据:同一个 summary 还被 trace 与会话落库引用。"""
        summary = {"answer_source": "compiled"}
        event = {"content": "答案", "summary": summary}
        with_answer_source(event)
        assert summary == {"answer_source": "compiled"}
        assert event == {"content": "答案", "summary": summary}
