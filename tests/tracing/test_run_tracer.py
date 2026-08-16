"""RunTracer tests — span tree + rich run log + verbose echo.

RunTracer 是 per-run 三合一观测层:
  1. span 树事件(span_start/span_end/llm/tool)写入 traces.jsonl,带 parent 层级
  2. 人类可读的详尽 run 日志 runs/{run_id}.log(完整 prompt/输出/工具观测)
  3. 可选 verbose 控制台回显
"""

import io

from trove.tracing.runlog import MAX_RUN_LOGS, create_tracer, get_tracer
from trove.tracing.local import configure_trace_store, get_run


def _events(run_id):
    return get_run(run_id)["events"]


class TestRunTracerSpanTree:
    def test_span_tree_events_and_run_log_content(self, tmp_path):
        """一次 run:节点 span 包裹 llm/tool 子事件,parent 链正确;
        run 日志含完整 prompt、完整输出、工具观测与节点输入输出。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r1")
        tracer.start_run({"question": "有多少贷款？", "model": "deepseek/deepseek-chat", "lang": "zh"})
        sid = tracer.node_start("route_intent", {"question": "有多少贷款？"})
        tracer.llm(
            "route_intent", "deepseek/deepseek-chat",
            [
                {"role": "system", "content": "分类：query 或 metadata。"},
                {"role": "user", "content": "有多少贷款？"},
            ],
            "query", 327, temperature=0.0,
        )
        tracer.tool("get_table_columns", {"table": "loan"}, "account_id INTEGER, date DATE")
        tracer.node_end(sid, {"intent": "query", "intent_evidence": {"llm_verdict": "query"}})
        tracer.finish({"verdict": "OK", "retry_count": 0})

        events = _events("r1")
        assert [e["kind"] for e in events] == [
            "run", "span_start", "llm", "tool", "span_end", "finish",
        ]

        span_start, llm_ev, tool_ev, span_end = events[1:5]
        assert llm_ev["parent_id"] == span_start["span_id"]
        assert tool_ev["parent_id"] == span_start["span_id"]
        assert span_end["span_id"] == span_start["span_id"]
        assert llm_ev["messages"][0]["content"] == "分类：query 或 metadata。"
        assert llm_ev["temperature"] == 0.0
        assert tool_ev["observation"] == "account_id INTEGER, date DATE"
        assert span_start["name"] == "route_intent"
        assert span_start["input"]["question"] == "有多少贷款？"
        assert span_end["output"]["intent"] == "query"

        log_text = (tmp_path / "runs" / "r1.log").read_text(encoding="utf-8")
        assert "有多少贷款？" in log_text
        assert "分类：query 或 metadata。" in log_text  # 完整 system prompt 不截断
        assert "account_id INTEGER, date DATE" in log_text  # 工具观测
        assert "intent_evidence" in log_text
        assert "verdict" in log_text

    def test_nested_spans_for_retried_nodes(self, tmp_path):
        """同一节点重跑两次 → 两个独立 span,每次执行自己的输入输出。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r2")
        tracer.start_run({"question": "q"})
        s1 = tracer.node_start("gen_sql", {"retry_count": 0})
        tracer.node_end(s1, {"sql": "SELECT 1"})
        s2 = tracer.node_start("gen_sql", {"retry_count": 1})
        tracer.node_end(s2, {"sql": "SELECT 2"})

        spans = [e for e in _events("r2") if e["kind"] == "span_start"]
        ends = [e for e in _events("r2") if e["kind"] == "span_end"]
        assert len(spans) == 2 and len(ends) == 2
        assert spans[0]["span_id"] != spans[1]["span_id"]
        assert spans[0]["input"]["retry_count"] == 0
        assert spans[1]["input"]["retry_count"] == 1
        tracer.finish({})

    def test_step_events_kept_for_trace_replay(self, tmp_path):
        """step 事件继续写入(向后兼容 /trace 回放)。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r3")
        tracer.start_run({"question": "q"})
        tracer.step({
            "type": "step", "seq": 1, "node": "gen_sql", "elapsed_ms": 5,
            "detail": {"sql": "SELECT 1"},
        })
        tracer.finish({"verdict": "OK"})
        assert any(e["kind"] == "step" for e in _events("r3"))

    def test_tracer_unregistered_on_finish(self, tmp_path):
        configure_trace_store(tmp_path)
        tracer = create_tracer("r4")
        assert get_tracer("r4") is tracer
        tracer.start_run({})
        tracer.finish({})
        assert get_tracer("r4") is None

    def test_long_values_truncated_in_span_and_log(self, tmp_path):
        """大字段(rows/schema_context)预览截断,不写整表进 trace。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r5")
        tracer.start_run({"question": "q"})
        sid = tracer.node_start("execute_sql", {"sql": "SELECT * FROM loan"})
        tracer.node_end(sid, {"rows": [[str(i)] for i in range(500)]})
        tracer.finish({})

        span_end = next(e for e in _events("r5") if e["kind"] == "span_end")
        assert len(span_end["output"]["rows"]) < 100
        log_text = (tmp_path / "runs" / "r5.log").read_text(encoding="utf-8")
        assert "…" in log_text  # 截断标记


class TestRunTracerVerbose:
    def test_verbose_echoes_sections_to_stream(self, tmp_path):
        configure_trace_store(tmp_path)
        buf = io.StringIO()
        tracer = create_tracer("r6", verbose=True, stream=buf)
        tracer.start_run({"question": "q"})
        sid = tracer.node_start("planner", {"question": "q"})
        tracer.llm("planner", "m", [{"role": "user", "content": "hello"}], "plan text", 10)
        tracer.node_end(sid, {"plan": "plan text"})
        tracer.finish({"verdict": "OK"})

        out = buf.getvalue()
        assert "planner" in out
        assert "hello" in out
        assert "plan text" in out

    def test_non_verbose_stays_silent(self, tmp_path):
        configure_trace_store(tmp_path)
        buf = io.StringIO()
        tracer = create_tracer("r7", stream=buf)
        tracer.start_run({"question": "q"})
        sid = tracer.node_start("planner", {})
        tracer.node_end(sid, {})
        tracer.finish({})
        assert buf.getvalue() == ""


class TestRunTracerLifecycle:
    def test_run_logs_trimmed_to_max(self, tmp_path):
        configure_trace_store(tmp_path)
        for i in range(MAX_RUN_LOGS + 2):
            tracer = create_tracer(f"r{i}")
            tracer.start_run({"question": f"q{i}"})
            tracer.finish({})
        logs = list((tmp_path / "runs").glob("*.log"))
        assert len(logs) <= MAX_RUN_LOGS
        assert not (tmp_path / "runs" / "r0.log").exists()
        assert (tmp_path / "runs" / f"r{MAX_RUN_LOGS + 1}.log").exists()

    def test_no_store_configured_is_noop(self, tmp_path):
        """未配置 trace store 时:不落盘、不崩溃(测试/CI 环境)。"""
        configure_trace_store(None)
        tracer = create_tracer("r8")
        tracer.start_run({"question": "q"})
        sid = tracer.node_start("x", {})
        tracer.llm("x", "m", [], "", 1)
        tracer.node_end(sid, {})
        tracer.finish({})


class TestRunTracerInputNormalization:
    def test_node_start_accepts_pydantic_state(self, tmp_path):
        """LangGraph 回调传入的是 Pydantic 状态模型(非 dict)——
        node_start/node_end 必须归一化成 dict 再落盘。"""
        from pydantic import BaseModel

        class StateModel(BaseModel):
            question: str = "q"
            intent: str = "query"

        configure_trace_store(tmp_path)
        tracer = create_tracer("r9")
        tracer.start_run({"question": "q"})
        sid = tracer.node_start("route_intent", StateModel())
        tracer.node_end(sid, StateModel().model_dump())
        tracer.finish({})

        span = next(e for e in _events("r9") if e["kind"] == "span_start")
        assert span["input"]["question"] == "q"
        assert span["input"]["intent"] == "query"
        log_text = (tmp_path / "runs" / "r9.log").read_text(encoding="utf-8")
        assert "question = q" in log_text


class TestRunLogFreshness:
    def test_same_run_id_restarts_log_file(self, tmp_path):
        """同一 run_id 重跑(eval-1 反复评估):新 run 覆盖旧日志,不残留上次内容。"""
        configure_trace_store(tmp_path)
        t1 = create_tracer("r10")
        t1.start_run({"question": "old question"})
        t1.llm("route_intent", "m", [{"role": "user", "content": "old prompt"}], "old", 1)
        t1.finish({})

        t2 = create_tracer("r10")
        t2.start_run({"question": "new question"})
        t2.llm("route_intent", "m", [{"role": "user", "content": "new prompt"}], "new", 1)
        t2.finish({})

        log_text = (tmp_path / "runs" / "r10.log").read_text(encoding="utf-8")
        assert "new question" in log_text
        assert "new prompt" in log_text
        assert "old question" not in log_text
        assert "old prompt" not in log_text

    def test_llm_message_content_kept_full(self, tmp_path):
        """run 日志里 LLM 的完整 prompt 不截断(超长 schema context 也要全量)。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r11")
        tracer.start_run({"question": "q"})
        long_prompt = "SCHEMA " + "x" * 3000 + " TAIL_MARKER"
        tracer.llm("gen_sql", "m", [{"role": "user", "content": long_prompt}], "SELECT 1", 1)
        tracer.finish({})

        log_text = (tmp_path / "runs" / "r11.log").read_text(encoding="utf-8")
        assert "TAIL_MARKER" in log_text  # 尾部都在 = 完整保留


class TestSpanCallbackDedupe:
    def test_callback_skips_duplicate_same_node_event(self, tmp_path):
        """LangGraph 对同一节点会重复触发 callback(节点函数链)——同栈顶
        同名节点只开一个 span,避免冗余嵌套。"""
        import asyncio

        configure_trace_store(tmp_path)
        tracer = create_tracer("r12")
        tracer.start_run({"question": "q"})
        handler = tracer.callback()

        asyncio.run(handler.on_chain_start(
            None, {"question": "q"}, run_id="c1",
            metadata={"langgraph_node": "gen_sql"}))
        # 同节点第二次触发:栈顶同名 → 跳过,不开新 span
        asyncio.run(handler.on_chain_start(
            None, {"question": "q"}, run_id="c2",
            metadata={"langgraph_node": "gen_sql"}))
        asyncio.run(handler.on_chain_end({"sql": "SELECT 1"}, run_id="c2"))
        asyncio.run(handler.on_chain_end({"sql": "SELECT 1"}, run_id="c1"))
        tracer.finish({})

        spans = [e for e in _events("r12") if e["kind"] == "span_start"]
        assert len(spans) == 1

    def test_callback_ignores_root_and_boundary_nodes(self, tmp_path):
        import asyncio

        configure_trace_store(tmp_path)
        tracer = create_tracer("r13")
        tracer.start_run({"question": "q"})
        handler = tracer.callback()
        asyncio.run(handler.on_chain_start(None, {}, run_id="c1", metadata={}))
        asyncio.run(handler.on_chain_start(None, {}, run_id="c2", metadata={"langgraph_node": "__start__"}))
        tracer.finish({})
        assert not [e for e in _events("r13") if e["kind"] == "span_start"]


class TestRunLogHierarchy:
    def test_nested_spans_rendered_as_tree(self, tmp_path):
        """分层渲染:子 span(子图节点)带 │ 深度缩进;节点段 ├─ 开头 └─ 收尾。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r14")
        tracer.start_run({"question": "q"})
        outer = tracer.node_start("gen_sql", {})
        inner = tracer.node_start("generate", {})  # 嵌套子 span
        tracer.llm("gen_sql", "m", [{"role": "user", "content": "hi"}], "SELECT 1", 1)
        tracer.node_end(inner, {"sql": "SELECT 1"})
        tracer.node_end(outer, {"sql": "SELECT 1"})
        tracer.finish({"verdict": "OK"})

        lines = (tmp_path / "runs" / "r14.log").read_text(encoding="utf-8").splitlines()
        gen_line = next(l for l in lines if "gen_sql" in l and "├─ [" in l)
        generate_line = next(l for l in lines if "generate" in l and "├─ [" in l)
        assert gen_line.startswith("├─ [")       # 顶层节点无缩进
        assert generate_line.startswith("│ ├─ [")  # 子节点带祖先连接符
        assert any("├─ in:" in l for l in lines)
        assert any("└─ out" in l for l in lines)
        assert any("└─ finish" in l for l in lines)
        # llm 事件缩进在节点之下
        llm_line = next(l for l in lines if "· llm" in l)
        assert llm_line.startswith("│ ") or llm_line.startswith("├─ · llm")

    def test_verbose_echo_has_tree_hierarchy(self, tmp_path):
        """verbose 回显与 run 日志同构:同样带层级连接符。"""
        import io

        configure_trace_store(tmp_path)
        buf = io.StringIO()
        tracer = create_tracer("r15", verbose=True, stream=buf)
        tracer.start_run({"question": "q"})
        outer = tracer.node_start("planner", {})
        tracer.tool("get_table_columns", {"table": "loan"}, "cols")
        tracer.node_end(outer, {"plan": "p"})
        tracer.finish({})

        out = buf.getvalue()
        assert "├─ [" in out
        assert "└─ out" in out
        assert "· tool get_table_columns" in out


class TestRunTracerRobustness:
    def test_broken_verbose_stream_never_breaks_pipeline(self, tmp_path):
        """管道被消费方关闭(head/grep -q):回显异常被吞掉并关闭回显,
        span 栈与 run 日志不受影响,主流程绝不中断。"""
        configure_trace_store(tmp_path)

        class BrokenStream:
            def write(self, s):
                raise BrokenPipeError("pipe closed")

            def flush(self):
                pass

        tracer = create_tracer("r16", verbose=True, stream=BrokenStream())
        tracer.start_run({"question": "q"})
        sid = tracer.node_start("gen_sql", {})
        tracer.llm("gen_sql", "m", [{"role": "user", "content": "hi"}], "SELECT 1", 1)
        tracer.node_end(sid, {"sql": "SELECT 1"})
        tracer.finish({"verdict": "OK"})

        # 日志文件完整写入(回显失败不影响落盘)
        log_text = (tmp_path / "runs" / "r16.log").read_text(encoding="utf-8")
        assert "├─ [1] gen_sql" in log_text
        assert "SELECT 1" in log_text

    def test_finish_is_idempotent(self, tmp_path):
        """eval 崩溃路径可能重复调用 finish(如 OK 后再 CRASH):只记第一次。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r17")
        tracer.start_run({"question": "q"})
        tracer.finish({"verdict": "OK"})
        tracer.finish({"verdict": "CRASH"})
        finishes = [e for e in _events("r17") if e["kind"] == "finish"]
        assert len(finishes) == 1
        assert finishes[0]["summary"]["verdict"] == "OK"
        # 日志页脚也只写一次
        log_text = (tmp_path / "runs" / "r17.log").read_text(encoding="utf-8")
        assert log_text.count("└─ finish") == 1
