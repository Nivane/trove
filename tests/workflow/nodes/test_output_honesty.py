"""批次 1a:答案不再骗人 —— output 节点的诚实性断言。

四条纪律各一组:

* ANS-1 截断披露:结果被 ``max_rows`` 削过时,不许承诺「下载为完整结果」;
* ANS-4 归因降级:degraded 的归因不许只留一个空标题;
* ANS-6 零行结果:0 行要给出可执行的原因/下一步,而不是一句冷冰冰的计数;
* ERR-3/4 内部词汇:澄清标题跟随语言,判定档位说人话、外语原因不进中文答案。

这里直接构造状态(``output`` 是纯渲染面);节点侧写状态的部分
(insights/conclusion/attribution/chart)由各自的测试文件钉。
"""

from __future__ import annotations

from trove.workflow.nodes.output import _build_results_table, output
from trove.workflow.state import WorkflowState


def make_state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1",
        "question": "哪些地区的贷款金额最高?",
        "lang": "zh",
        "sql": "SELECT region, amount FROM loan",
        "columns": ["region", "amount"],
        "rows": [["华东", 100]],
        "row_count": 1,
        "verdict": "OK",
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


# ── ANS-1 截断披露 ────────────────────────────────────────


class TestTruncatedResultDisclosure:
    def test_capped_rows_do_not_promise_full_download(self):
        """rows 已被 max_rows 截断 → 下载也不可能是完整结果。

        判定依据是 ``len(rows) < row_count``(真实总数 > 手里保留的行数),
        而不是 display_rows —— 后者只决定表格渲染几行。
        """
        table = _build_results_table(
            "zh", ["x"], [[1], [2]], row_count=10, display_rows=50,
        )
        assert "下载为完整查询结果" not in table
        assert "仅保留前 2 行" in table

    def test_uncapped_rows_keep_download_promise(self):
        """rows 完整保留(只是表格展示上限)→ 下载承诺仍然成立。"""
        rows = [[i] for i in range(60)]
        table = _build_results_table(
            "zh", ["x"], rows, row_count=60, display_rows=50,
        )
        assert "下载为完整查询结果" in table
        assert "仅保留前" not in table

    def test_capped_rows_english(self):
        table = _build_results_table(
            "en", ["x"], [[1], [2]], row_count=10, display_rows=50,
        )
        assert "download includes the full result" not in table
        assert "only the first 2 rows" in table


# ── ANS-4 归因降级 ────────────────────────────────────────


class TestDegradedAttributionVisible:
    async def test_degraded_attribution_explains_instead_of_bare_heading(self):
        state = make_state(attribution={
            "table": [],
            "hops": [{"hop": 0, "sql": "SELECT 1"}],
            "narrative": "",
            "baseline": "prev_period",
            "degraded": True,
        })
        response = (await output(state))["final_response"]
        assert "归因分析未能完成" in response
        assert "部分证据" in response

    async def test_healthy_attribution_has_no_failure_line(self):
        state = make_state(attribution={
            "table": [{"dim": "华东", "base": 100, "current": 80,
                       "delta": -20, "contribution": -0.6}],
            "narrative": "主要是华东贡献",
            "baseline": "prev_period",
        })
        response = (await output(state))["final_response"]
        assert "归因分析未能完成" not in response

    async def test_degraded_attribution_english(self):
        state = make_state(
            lang="en",
            attribution={"table": [], "narrative": "", "degraded": True},
        )
        response = (await output(state))["final_response"]
        assert "Attribution could not be completed" in response


# ── ANS-5 图表失败可见 ────────────────────────────────────


class TestChartFailureVisible:
    async def test_chart_failure_line_in_details(self):
        state = make_state(chart_status="failed")
        response = (await output(state))["final_response"]
        assert "未能生成图表" in response

    async def test_no_chart_failure_line_without_status(self):
        response = (await output(make_state()))["final_response"]
        assert "未能生成图表" not in response

    async def test_chart_failure_english(self):
        state = make_state(lang="en", question="Which regions?",
                           chart_status="failed")
        response = (await output(state))["final_response"]
        assert "could not be generated" in response


# ── ANS-6 零行结果 ────────────────────────────────────────


class TestZeroRowsExplain:
    async def test_zero_rows_with_columns_gives_actionable_hint(self):
        state = make_state(columns=["region"], rows=[], row_count=0)
        response = (await output(state))["final_response"]
        assert "结果 (0 行)" in response
        assert "筛选条件" in response
        assert "放宽" in response

    async def test_zero_rows_without_columns_keeps_count_and_hint(self):
        state = make_state(columns=[], rows=[], row_count=0)
        response = (await output(state))["final_response"]
        assert "查询返回 0 行" in response
        assert "筛选条件" in response

    async def test_no_execution_has_no_zero_row_guidance(self):
        """row_count == -1 是「没执行」,不是「查出 0 行」—— 不许套用 0 行的解释。"""
        state = make_state(columns=[], rows=[], row_count=-1)
        response = (await output(state))["final_response"]
        assert "查询返回 0 行" not in response
        assert "筛选条件" not in response

    async def test_zero_rows_english(self):
        state = make_state(
            lang="en", question="Which regions?", columns=["region"],
            rows=[], row_count=0,
        )
        response = (await output(state))["final_response"]
        assert "filters" in response
        assert "time range" in response


# ── ANS-2/3 生成失败可见 ──────────────────────────────────


class TestGenerationFailureVisible:
    async def test_conclusion_failure_is_visible(self):
        state = make_state(conclusion_status="failed")
        response = (await output(state))["final_response"]
        assert "结论生成失败" in response

    async def test_insights_failure_is_visible(self):
        state = make_state(insights_status="failed")
        response = (await output(state))["final_response"]
        assert "洞察生成失败" in response

    async def test_absent_status_adds_no_notice(self):
        response = (await output(make_state()))["final_response"]
        assert "生成失败" not in response

    async def test_insights_failure_english(self):
        state = make_state(lang="en", question="Which regions?",
                           insights_status="failed")
        response = (await output(state))["final_response"]
        assert "Insight generation failed" in response

    async def test_conclusion_failure_english(self):
        state = make_state(lang="en", question="Which regions?",
                           conclusion_status="failed")
        response = (await output(state))["final_response"]
        assert "Conclusion generation failed" in response


# ── ERR-3 澄清标题 ────────────────────────────────────────


class TestClarificationLocalized:
    async def test_zh_clarification_uses_chinese_title(self):
        state = make_state(clarification_question="你指的是哪个地区?")
        response = (await output(state))["final_response"]
        assert "## Clarification" not in response
        assert "## 澄清" in response
        assert "**Question**" not in response
        assert "**问题**" in response

    async def test_en_clarification_unchanged(self):
        state = make_state(
            lang="en", question="Which region?",
            clarification_question="Do you mean the loan issue date?",
        )
        response = (await output(state))["final_response"]
        assert "## Clarification" in response
        assert "**Question**" in response


# ── ERR-4 判定档位 ────────────────────────────────────────


class TestVerdictLocalized:
    async def test_zh_verdict_label_is_human_and_foreign_reason_suppressed(self):
        state = make_state(verdict="RETRY", reason="ordering rule failed")
        response = (await output(state))["final_response"]
        assert "RETRY" not in response
        assert "自动校验未通过" in response
        assert "ordering rule failed" not in response

    async def test_zh_verdict_keeps_chinese_reason(self):
        state = make_state(verdict="RETRY", reason="排序与问题不符")
        response = (await output(state))["final_response"]
        assert "自动校验未通过" in response
        assert "排序与问题不符" in response

    async def test_en_verdict_label_and_reason(self):
        state = make_state(
            lang="en", question="Which regions?",
            verdict="RETRY", reason="ordering rule failed",
        )
        response = (await output(state))["final_response"]
        assert "RETRY" not in response
        assert "automatic validation did not pass" in response
        assert "ordering rule failed" in response

    async def test_ok_verdict_has_no_line(self):
        state = make_state(verdict="OK")
        response = (await output(state))["final_response"]
        assert "评估" not in response
        assert "Assessment" not in response
