"""装配 dump 端到端测试 —— reflection 全图跑一遍,断言 assembly.json 落盘且三节成形。

零 LLM / 零网络:脚本化 LLM 按 prompt 内容分发(intent → "query"、
reflect 裁决 → "OK"、生成 → SQL);agent loop 首轮调用一次
``validate_sql``(让 tools 节的 calls 有真实计数),次轮 content-only 收尾。

这里断言的是**真实 run 产出的清单形状**,与 tests/tracing/test_assembly.py
的序列化单测互补:单测钉字段与降级,这里钉"图跑完之后文件真的在 .log 旁边、
三节按 schema 成形"。
"""

from __future__ import annotations

import json

import pytest

from tests.helpers.kb import ossie_semantics_yaml
from trove.agent.session import SessionManager
from trove.core.config import AgentConfig
from trove.services.datasource.catalog import CatalogService
from trove.services.kb.service import KbService
from trove.storage.session_store import SessionStore
from trove.tracing.assembly import BLOCK_NAMES
from trove.tracing.local import configure_trace_store
from trove.workflow.graphs import GraphServices, build_graphs

SQL = (
    "SELECT d.A2 AS district_name, AVG(l.amount) AS avg_loan "
    "FROM loan l "
    "JOIN account a ON l.account_id = a.account_id "
    "JOIN district d ON a.district_id = d.district_id "
    "GROUP BY d.A2 ORDER BY avg_loan DESC"
)

# 两个问题各自的 KB 术语(确定性 generated 语义之外的锚),保证
# gen_assemble 的 term_notes 块非空 —— blocks 节有真实条目可断言。
_TERMS = [
    {"term": "平均贷款金额", "aliases": ["平均贷款"],
     "mapping": "AVG(loan.amount)", "tables": ["loan", "account", "district"],
     "definition": "按地区分组的贷款金额均值"},
    {"term": "贷款笔数", "aliases": ["贷款数量"],
     "mapping": "COUNT(*)", "tables": ["loan"],
     "definition": "发放的贷款记录条数"},
]


def _last_user_text(messages) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def _system_text(messages) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )


class AssemblyScriptedLLM:
    """内容分发的脚本化 LLM(与 tests/test_integration.py 的 ScriptedLLM 同型)。

    - ``chat``:意图分类器 → "query";reflect 裁决 → "OK";其余 → SQL。
    - ``chat_full``:agent loop 首轮(带 tools)给一次 ``validate_sql`` 工具
      调用,之后 content-only 交付 SQL(模型自定终止)。
    """

    def __init__(self, sql: str = SQL):
        self.sql = sql
        self.chat_calls = 0
        self.full_calls = 0

    async def chat(self, model: str, messages: list[dict], **kwargs) -> str:
        self.chat_calls += 1
        text = _last_user_text(messages)
        system = _system_text(messages)
        if "把用户输入分类为以下之一" in system:
            return "query"
        if "Does this result correctly answer" in text:
            return "OK"
        return f"```sql\n{self.sql}\n```"

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.full_calls += 1
        if tools and self.full_calls == 1:
            return {
                "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "name": "validate_sql",
                    "arguments": json.dumps({"sql": self.sql}),
                }],
            }
        return {"content": f"```sql\n{self.sql}\n```", "tool_calls": []}


@pytest.fixture
async def assembly_stack(tmp_path, demo_registry):
    """带 trace store + KB 术语的完整栈(agentic 生成 + reflection 图)。"""
    configure_trace_store(tmp_path)

    store = SessionStore(home_dir=str(tmp_path / "home"))
    catalog = CatalogService(demo_registry)
    config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")
    llm = AssemblyScriptedLLM()

    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / demo_registry.default_name).mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / demo_registry.default_name / "semantics.yml").write_text(
        ossie_semantics_yaml(_TERMS), encoding="utf-8",
    )

    services = GraphServices(
        llm=llm,
        catalog=catalog,
        connectors=demo_registry,
        semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
        config=config,
        kb=kb,
    )
    manager = SessionManager(
        config=config,
        session_store=store,
        # agentic=True(默认)+ query_sketch=False:plan_json 为 None →
        # complexity=standard → 走 agent loop,tools 节才有内容可查。
        graphs=build_graphs(services, query_sketch=False),
        llm_gateway=llm,
    )
    yield manager, tmp_path, demo_registry
    # 后端连接 dispose:aiosqlite 常驻线程不关闭会挂住进程退出(与
    # conftest.py 的 session_manager 夹具同款处置)。
    await store.dispose()


def _load_dump(runs_dir, run_id: str) -> dict:
    path = runs_dir / f"{run_id}.assembly.json"
    assert path.exists(), f"no assembly dump at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


class TestReflectionRunDropsAssembly:
    """reflection 全图跑一遍 → runs/<run_id>.assembly.json 与 .log 并列落盘。"""

    async def test_dump_lands_next_to_log_with_three_sections(
        self, assembly_stack,
    ):
        manager, tmp_path, registry = assembly_stack
        session = await manager.start_session(project_cwd=str(tmp_path))
        state = await manager.ask(
            session=session,
            question="哪个地区的平均贷款金额最高？",
            workflow_name="reflection",
            datasource=registry.default_name,
        )
        assert state.error == ""
        assert state.verdict == "OK"

        runs_dir = tmp_path / "runs"
        assert (runs_dir / f"{state.run_id}.log").exists()
        report = _load_dump(runs_dir, state.run_id)

        # 顶层字段一次定死
        assert set(report) == {
            "run_id", "datasource", "complexity", "blocks", "tools", "verdicts",
        }
        assert report["run_id"] == state.run_id
        assert report["datasource"] == registry.default_name
        assert report["complexity"] in ("simple", "standard", "complex")

        # ── blocks 节:块与条目都是定死的形状 ──
        assert report["blocks"], "gen 装配至少产出一个上下文块"
        names = [b["name"] for b in report["blocks"]]
        assert set(names) <= set(BLOCK_NAMES)
        assert "term_notes" in names  # KB 术语命中 → 该块必有
        for block in report["blocks"]:
            assert set(block) == {"name", "tokens", "truncated", "items"}
            assert isinstance(block["tokens"], int) and block["tokens"] > 0
            assert isinstance(block["truncated"], bool)
            for item in block["items"]:
                assert set(item) == {"ref", "tokens", "truncated"}
                assert isinstance(item["ref"], str) and item["ref"]
                assert isinstance(item["tokens"], int)
                assert isinstance(item["truncated"], bool)
        terms = next(b for b in report["blocks"] if b["name"] == "term_notes")
        assert terms["items"], "term_notes 块应带逐条明细"
        assert any("平均贷款金额" in it["ref"] for it in terms["items"])

        # ── tools 节:注册表逐工具形状 + 真实调用计数 ──
        assert report["tools"], "agentic 轮必然建了工具注册表"
        for tool in report["tools"]:
            assert set(tool) == {
                "name", "level", "roles", "lazy", "activated", "calls",
            }
            assert isinstance(tool["name"], str) and tool["name"]
            assert tool["level"] in ("core", "catalog", "admin")
            assert tool["roles"] is None or isinstance(tool["roles"], list)
            assert isinstance(tool["lazy"], bool)
            assert isinstance(tool["activated"], bool)
            assert isinstance(tool["calls"], int) and tool["calls"] >= 0
        by_name = {t["name"]: t for t in report["tools"]}
        assert by_name["validate_sql"]["calls"] == 1  # 脚本里的那一轮工具调用
        assert by_name["validate_sql"]["lazy"] is False

        # ── verdicts 节:四类判定直读 state ──
        verdicts = report["verdicts"]
        assert set(verdicts) == {
            "validator_hits", "guard_hits", "rule_hits", "fast_path_hit",
        }
        assert isinstance(verdicts["validator_hits"], list)
        assert isinstance(verdicts["guard_hits"], list)
        assert isinstance(verdicts["rule_hits"], list)
        assert verdicts["fast_path_hit"] is False  # 无 KB 模板 → 未走快径

        # context_usage 保持旧传输形状(SSE/前端契约):明细只进 dump
        for usage in state.context_usage:
            assert set(usage) == {
                "name", "tokens", "included", "items_total", "items_included",
            }

    async def test_second_run_gets_own_dump_with_history_block(
        self, assembly_stack,
    ):
        """同一 session 第二问:新 run_id 新清单,history 块带逐轮条目。"""
        manager, tmp_path, registry = assembly_stack
        session = await manager.start_session(project_cwd=str(tmp_path))

        first = await manager.ask(
            session=session, question="哪个地区的平均贷款金额最高？",
            workflow_name="reflection", datasource=registry.default_name,
        )
        second = await manager.ask(
            session=session, question="贷款笔数是多少？",
            workflow_name="reflection", datasource=registry.default_name,
        )
        assert first.run_id != second.run_id

        runs_dir = tmp_path / "runs"
        report = _load_dump(runs_dir, second.run_id)
        assert (runs_dir / f"{first.run_id}.assembly.json").exists()  # 旧清单不被覆盖

        blocks = {b["name"]: b for b in report["blocks"]}
        assert "history" in blocks, "第二问的装配必须带上一轮历史"
        refs = [it["ref"] for it in blocks["history"]["items"]]
        assert refs, "历史块应带逐轮条目"
        assert any(r.startswith("turn") for r in refs)  # key 回退即位置标识


class TestDumpOverwriteSemantics:
    """同一 run_id 重跑覆盖(与 .log 同款语义)。"""

    async def test_rerun_same_run_id_overwrites(self, assembly_stack):
        manager, tmp_path, registry = assembly_stack
        session = await manager.start_session(project_cwd=str(tmp_path))
        state = await manager.ask(
            session=session, question="贷款笔数是多少？", workflow_name="reflection",
            datasource=registry.default_name,
        )
        runs_dir = tmp_path / "runs"
        path = runs_dir / f"{state.run_id}.assembly.json"
        assert path.exists()

        # 手工篡改后按同一 run_id 重写 → 覆盖,文件数不增
        path.write_text("{}", encoding="utf-8")
        from trove.tracing.assembly import write_report

        write_report(state.run_id, {"run_id": state.run_id, "datasource": "demo"})
        assert json.loads(path.read_text(encoding="utf-8"))["datasource"] == "demo"
        assert len(list(runs_dir.glob(f"{state.run_id}.*"))) == 2  # .log + dump
