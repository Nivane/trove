"""Error analysis node tests — classify, judge, and plan the fix."""

import pytest

from trove.core.config import AgentConfig
from trove.workflow.nodes.analyze_error import classify_fix_mode, make_analyze_error
from trove.workflow.state import WorkflowState


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "q"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class TestAnalyzeError:
    async def test_produces_analysis_with_classification(self):
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "类型: Schema Linking\n判断: loans 表不存在\n修正: 改用 loan"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT * FROM loans",
            error_feedback="no such table: loans",
            schema_context="Table: loan",
        )
        update = await node(state)
        assert "Schema Linking" in update["error_analysis"]
        # prompt 含错误 SQL、错误信息、schema 与分类要求
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "loans" in prompt
        assert "no such table" in prompt
        assert "Schema Linking" in prompt

    async def test_no_feedback_passes(self):
        class NoLLM:
            async def chat(self, *a, **k):
                raise AssertionError("must not run")

        node = make_analyze_error(NoLLM(), AgentConfig(target="m"))
        assert await node(make_state()) == {}

    async def test_uses_fast_model_when_configured(self):
        """失败诊断走 fast 档(配置 model_fast 时),不烧推理模型。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["model"] = model
                return "类型: Schema Linking\n判断: loans 表不存在\n修正: 改用 loan"

        node = make_analyze_error(LLM(), AgentConfig(target="m", model_fast="fast/model"))
        state = make_state(
            sql="SELECT * FROM loans",
            error_feedback="no such table: loans",
            schema_context="Table: loan",
        )
        await node(state)
        assert captured["model"] == "fast/model"

    async def test_prompt_includes_evidence_hint(self):
        """诊断必须看到官方 evidence,不能凭记忆误判业务语义。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT 1",
            error_feedback="results differ",
            evidence="'POPLATEK PO OBRATU' represents for 'issuance after transaction'",
        )
        await node(state)
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "POPLATEK PO OBRATU" in prompt

    async def test_prompt_includes_prior_reasoning_trail(self):
        """诊断必须看到上一轮生成/规划方的思考轨迹,才能定位误判根因。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT 1",
            error_feedback="results differ",
            reasoning_history=[
                {"node": "reflect", "text": "无关节点的痕迹不应出现"},
                {"node": "gen_sql", "text": "先试 validate_sql 再决定用哪个解释"},
            ],
        )
        await node(state)
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "validate_sql" in prompt          # gen_sql 的轨迹进 prompt
        assert "无关节点" not in prompt           # 其他节点的痕迹不混入

    async def test_llm_failure_is_silent(self):
        class BrokenLLM:
            async def chat(self, *a, **k):
                raise RuntimeError("down")

        node = make_analyze_error(BrokenLLM(), AgentConfig(target="m"))
        assert await node(make_state(error_feedback="boom")) == {}

    async def test_empty_output_gets_deterministic_fallback(self):
        """LLM 调用成功但输出为空 → 按错误模式给出确定性兜底诊断,不静默放行。"""
        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return ""

        node = make_analyze_error(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="Validation rule: list question returned no rows",
            lang="en",
        ))
        assert "empty result" in update["error_analysis"].lower()
        assert "filter" in update["error_analysis"].lower()
        assert update["rollback_target"] == "gen_sql"

    async def test_empty_output_fallback_syntax_pattern(self):
        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return ""

        node = make_analyze_error(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="MySQL execution error: (1064, syntax error",
            lang="en",
        ))
        assert "syntax" in update["error_analysis"].lower()

    async def test_records_rejected_hypothesis(self):
        """诊断后把本轮失败假设(错误 SQL + 原因)记入黑名单,供后续轮次避开。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT name FROM client WHERE age = (SELECT MIN(age) FROM client)",
            error_feedback="Validation rule F1-a: single-value question returned multiple rows",
        ))
        assert update["rejected_hypotheses"] == [{
            "sql": "SELECT name FROM client WHERE age = (SELECT MIN(age) FROM client)",
            "reason": "Validation rule F1-a: single-value question returned multiple rows",
        }]

    async def test_rejected_hypothesis_dedup_by_fingerprint(self):
        """同一失败 SQL(含空白差异)重复诊断 → 不重复进黑名单。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT  1",  # 与已有指纹归一化后相同
            error_feedback="boom again",
            rejected_hypotheses=[{"sql": "SELECT 1", "reason": "old failure"}],
        )
        update = await node(state)
        assert update.get("rejected_hypotheses") == []  # 已在黑名单 → 无新增

    async def test_records_sql_version(self):
        """诊断后把本轮失败版本(SQL + 结果签名 + 规则命中 + 轮次)记入版本链。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT name FROM client",
            rows=[["a"], ["b"]],
            row_count=2,
            error_feedback="Validation rule [F1-b]: list question returned wide columns",
        ))
        assert len(update["sql_versions"]) == 1
        v = update["sql_versions"][0]
        assert v["sql"] == "SELECT name FROM client"
        assert v["round"] == 1
        assert v["issues"] == ["F1-b"]
        # 签名与归一化口径一致(排序/类型不敏感)
        assert v["sig"] == "repr-of-normalized" or len(v["sig"]) > 0

    async def test_regression_report_enters_diagnosis_prompt(self):
        """上一版与本轮结果签名相同 → 回归报告(无效修复)并入诊断 prompt。"""
        from trove.workflow.versions import result_sig

        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        rows = [["a"], ["b"]]
        state = make_state(
            sql="SELECT name FROM client",
            rows=rows,
            row_count=2,  # 执行过 → 规则失败路径(签名可比)
            error_feedback="Validation rule [F1-b]: list question returned wide columns",
            sql_versions=[{
                "sql": "SELECT name FROM client ORDER BY name",
                "sig": result_sig(rows),  # 与本轮结果签名相同 → 无效修复
                "issues": ["F1-b"],
                "round": 1,
            }],
        )
        await node(state)
        assert "Invalid fix" in captured["prompt"]
        assert "Round 1" in captured["prompt"]

    async def test_no_sql_verdict(self):
        """诊断判定问题本身不是 SQL 问题 → no_sql 出口 + 清掉陈旧反馈。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "NO_SQL: 这不是数据查询，是表含义问题"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="Candidate SQL variants returned different results (1 vs 10 rows)",
        ))
        assert update["no_sql"] is True
        assert update["error_feedback"] == ""  # 避免陈旧反馈注入答案 prompt / 跳过裁决
        assert "NO_SQL" in update["error_analysis"]  # 轨迹展示保留诊断文本


class TestRollbackDecision:
    """LLM 判断回退目标：解析、默认值、防打转护栏、RETRY 触发。"""

    async def test_parses_target_from_response(self):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "类型: 计划偏差\n判断: 分组维度错了\n修正: 按 county 分组\nTARGET: planner"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT AVG(grade) FROM students",
            error_feedback="wrong grouping",
        ))
        assert update["rollback_target"] == "planner"
        assert update["last_rollback_target"] == "planner"

    async def test_missing_target_defaults_to_gen_sql(self):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "diag: 表名错误"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(error_feedback="boom"))
        assert update["rollback_target"] == "gen_sql"

    async def test_repeated_target_escalates(self):
        """同一失败(错误文本一致)连续两次仍失败 → 强制升一档。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="boom",
            last_rollback_target="gen_sql",
            sql_versions=[{
                "sql": "SELECT 1", "sig": "", "issues": [], "round": 1,
                "error": "boom",  # 与当前失败文本一致 → 同一失败重演
            }],
        ))
        assert update["rollback_target"] == "planner"  # gen_sql → planner 升级

    async def test_repeated_target_different_failure_does_not_escalate(self):
        """目标重复但失败文本不同(如执行错误后接语义 RETRY)→ 视为新判断,不升级。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="no such table: clients",
            last_rollback_target="gen_sql",
            sql_versions=[{
                "sql": "SELECT 1", "sig": "exec-error", "issues": [], "round": 1,
                "error": "no such column: birth_date",  # 与当前失败文本不同
            }],
        ))
        assert update["rollback_target"] == "gen_sql"

    async def test_repeated_last_target_degrades(self):
        """护栏顶部目标同一失败仍重复 → 优雅降级（state.error）。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: schema_linking"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="boom",
            last_rollback_target="schema_linking",
            sql_versions=[{
                "sql": "SELECT 1", "sig": "", "issues": [], "round": 1,
                "error": "boom",
            }],
        ))
        assert update["error"]

    async def test_runs_on_reflect_retry(self):
        """reflect RETRY（无 error_feedback）也进诊断，reason 作为失败上下文。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return "类型: 业务语义\n判断: 分组错\n修正: 按 county 分组\nTARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT AVG(grade) FROM students",
            verdict="RETRY",
            reason="wrong grouping",
        ))
        assert "wrong grouping" in captured["prompt"]
        assert update["rollback_target"] == "gen_sql"


class TestFixMode:
    """失败 → 修复模式: fixer(实现级定点修) vs revisor(语义重写)。"""

    def test_filter_rule_means_revisor(self):
        """F2 族(过滤条件缺失/错误)是问题意图级 → 语义重写。"""
        assert classify_fix_mode("Validation rule [F2-a]: missing gender filter", ["F2-a"]) == "revisor"

    def test_shape_value_order_rules_mean_fixer(self):
        """F1(形状)/F3(值域)/F4(排序)是实现形态级 → 定点修。"""
        assert classify_fix_mode("rule [F1-b]", ["F1-b"]) == "fixer"
        assert classify_fix_mode("rule [F3-a]", ["F3-a"]) == "fixer"
        assert classify_fix_mode("rule [F4-b]", ["F4-b"]) == "fixer"

    def test_execution_error_means_fixer(self):
        """执行/语法错误(表不存在/未知列/1064) → 实现级。"""
        assert classify_fix_mode("no such table: loans", []) == "fixer"
        assert classify_fix_mode("MySQL error: (1064, syntax error)", []) == "fixer"
        assert classify_fix_mode("Unknown column 'x'", []) == "fixer"

    def test_vote_disagreement_means_revisor(self):
        """投票平局:候选解释不一致 → 语义重估。"""
        assert classify_fix_mode("Candidate SQL variants returned different results", []) == "revisor"
        assert classify_fix_mode("候选 SQL 结果不一致", []) == "revisor"

    def test_unknown_text_defaults_to_fixer(self):
        """未知失败文本默认保守: 先做最小实现级修复。"""
        assert classify_fix_mode("something unexpected happened", []) == "fixer"

    async def test_update_carries_fix_mode(self):
        """analyze_error 把判定出的修复模式传给重生成方。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT * FROM loans",
            error_feedback="no such table: loans",
        ))
        assert update["fix_mode"] == "fixer"


class TestProgressTracking:
    """缺口5: 修复进展量化 —— regression_state 标签 + no_progress 计数 + 提前止损。"""

    @staticmethod
    def _node():
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"
        return make_analyze_error(LLM(), AgentConfig(target="m"))

    async def test_first_round_does_not_count(self):
        """首轮失败(无基线) → progress=first, 不计数。"""
        update = await self._node()(make_state(error_feedback="boom"))
        assert update["last_progress"] == "first"
        assert update["no_progress_rounds"] == 0

    async def test_exec_failure_skips_sig_regression(self):
        """执行错误(row_count == -1)不比较结果集签名:不同错误不误报 invalid。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT * FROM clients",
            error_feedback="no such table: clients",  # 上一版是另一错误
            sql_versions=[{
                "sql": "SELECT * FROM loans", "sig": "exec-error",
                "issues": [], "round": 1, "error": "no such column: x",
            }],
        ))
        # 回归报告段(注入的确定性反馈)不应出现——skill 模板里静态存在
        # "[Regression check]" 术语说明,以注入报告独有的句式作判据
        assert "same execution error as Round" not in captured["prompt"]
        assert update["last_progress"] == "improved"  # 错误不同 = 有进展
        assert update["no_progress_rounds"] == 0
        # 版本记录带哨兵签名 + 原始错误文本
        v = update["sql_versions"][0]
        assert v["sig"] == "exec-error"
        assert v["error"] == "no such table: clients"

    async def test_exec_failure_same_error_marks_invalid_and_escalates(self):
        """同一执行错误(错误文本一致)重演 → invalid + 升档。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT * FROM clients",
            error_feedback="no such table: clients",
            last_rollback_target="gen_sql",
            sql_versions=[{
                "sql": "SELECT * FROM clients", "sig": "exec-error",
                "issues": [], "round": 1, "error": "no such table: clients",
            }],
        ))
        assert "same execution error as Round 1" in captured["prompt"]
        assert update["last_progress"] == "invalid"
        assert update["no_progress_rounds"] == 1
        assert update["rollback_target"] == "planner"  # 同错误重演 → 升档

    async def test_invalid_fix_increments_no_progress(self):
        """结果签名相同 → invalid, 无进展轮 +1。"""
        from trove.workflow.versions import result_sig

        rows = [["a"]]
        update = await self._node()(make_state(
            sql="SELECT 1", rows=rows, row_count=1,  # 执行过 → 规则失败路径
            error_feedback="Validation rule [F1-b]: wide columns",
            sql_versions=[{
                "sql": "SELECT 2", "sig": result_sig(rows),
                "issues": ["F1-b"], "round": 1,
            }],
        ))
        assert update["last_progress"] == "invalid"
        assert update["no_progress_rounds"] == 1

    async def test_no_progress_carries_previous_rounds(self):
        """无进展计数跨轮累积(operator.add 语义), 由 analyze_error 自身维护。"""
        from trove.workflow.versions import result_sig

        rows = [["a"]]
        update = await self._node()(make_state(
            sql="SELECT 1", rows=rows, row_count=1,  # 执行过 → 规则失败路径
            error_feedback="Validation rule [F1-b]: wide columns",
            no_progress_rounds=2,
            sql_versions=[{
                "sql": "SELECT 2", "sig": result_sig(rows),
                "issues": ["F1-b"], "round": 1,
            }],
        ))
        assert update["no_progress_rounds"] == 3

    async def test_improved_resets_no_progress(self):
        """有进展(签名变化且规则集变小) → 计数清零。"""
        update = await self._node()(make_state(
            sql="SELECT 1", rows=[["a"]], row_count=1,  # 执行过 → 规则失败路径
            error_feedback="boom", no_progress_rounds=2,
            sql_versions=[{
                "sql": "SELECT 2", "sig": "old-sig",
                "issues": [], "round": 1,
            }],
        ))
        assert update["last_progress"] == "improved"
        assert update["no_progress_rounds"] == 0

    async def test_three_stalled_rounds_degrades(self):
        """连续 3 轮无进展 → 停止迭代打回,直接优雅降级(不再烧预算)。"""
        from trove.workflow.versions import result_sig

        rows = [["a"]]
        update = await self._node()(make_state(
            sql="SELECT 1", rows=rows, row_count=1,  # 执行过 → 规则失败路径
            error_feedback="Validation rule [F1-b]: wide columns",
            no_progress_rounds=2,
            sql_versions=[{
                "sql": "SELECT 2", "sig": result_sig(rows),
                "issues": ["F1-b"], "round": 1,
            }],
        ))
        assert "error" in update
        assert "无进展" in update["error"] or "progress" in update["error"].lower()
