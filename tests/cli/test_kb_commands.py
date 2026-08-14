"""Knowledge base slash command tests (/kb init|list|reload|learn)."""

import pytest

from trove.cli.slash_registry import SlashRegistry
from trove.cli.commands.kb_cmds import register_kb_commands
from trove.core.config import AgentConfig
from trove.core.types import Message, Session
from trove.llm.gateway import LLMGateway
from trove.services.kb.service import KbService

DRAFT_YAML = """
example:
  question: 学生们的平均成绩是多少
  sql: SELECT county, AVG(grade) FROM students GROUP BY county
  tags: [成绩, 地区]
terms:
  - term: 平均成绩
    mapping: AVG(students.grade)
    tables: [students]
    definition: 学生平均分
"""


@pytest.fixture
def kb(tmp_path):
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    return kb


def make_reg(kb, **extra):
    reg = SlashRegistry()
    context = {
        "kb": kb,
        "config": AgentConfig(target="mock/model"),
        **extra,
    }
    register_kb_commands(reg, context)
    return reg


class TestRegistration:
    def test_kb_command_registered(self, kb):
        reg = make_reg(kb)
        cmd = reg.get("kb")
        assert cmd is not None
        assert cmd.group in {"session", "metadata", "system"}


class TestKbInit:
    async def test_init_generates_skeleton(self, kb, sqlite_registry):
        reg = make_reg(kb, connector_registry=sqlite_registry)
        result = await reg.get("kb").handler("init")
        assert "schema_notes.yml" in result
        ds = sqlite_registry.default_name
        assert (kb.kb_dir / ds / "schema_notes.yml").exists()
        assert "students" in (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")

    async def test_init_refuses_overwrite(self, kb, sqlite_registry):
        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "schema_notes.yml").write_text("tables: []\n", encoding="utf-8")
        reg = make_reg(kb, connector_registry=sqlite_registry)
        result = await reg.get("kb").handler("init")
        assert "refusing" in result
        assert (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8") == "tables: []\n"


THREE_DOC = """tables:
- name: students
  description: 学生表
  columns:
  - name: grade
    description: 成绩
    enums: []
  metrics: []
---
terms:
- term: 学生数
  aliases: []
  mapping: COUNT(students.id)
  tables: [students]
  definition: 学生记录数
---
examples:
- template: true
  question: 学生总数是多少
  sql: SELECT COUNT(*) FROM students
  tags: [学生]
"""

# 真实 LLM（DeepSeek）的自然输出：忽略 '---' 分隔，单文档三顶层键
MERGED_DOC = """tables:
- name: students
  description: 学生表
  columns:
  - name: grade
    description: 成绩
    enums: []
  metrics: []
terms:
- term: 学生数
  aliases: []
  mapping: COUNT(students.id)
  tables: [students]
  definition: 学生记录数
examples:
- template: true
  question: 学生总数是多少
  sql: SELECT COUNT(*) FROM students
  tags: [学生]
"""


class TestKbInitLLM:
    def _reg(self, kb, sqlite_registry, llm):
        reg = SlashRegistry()
        register_kb_commands(reg, {
            "kb": kb,
            "connector_registry": sqlite_registry,
            "llm_gateway": llm,
            "config": AgentConfig(target="mock/model"),
        })
        return reg

    async def test_init_generates_three_files_with_llm(self, kb, sqlite_registry):
        """有 LLM 时 /kb init 生成 schema_notes + semantics + examples 三份初稿。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=THREE_DOC))
        result = await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        assert "Initialized" in result
        assert (kb.kb_dir / ds / "schema_notes.yml").exists()
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert (kb.kb_dir / ds / "examples.yml").exists()
        assert "学生表" in (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "学生数" in (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")

    async def test_init_with_llm_repairs_broken_draft(self, kb, sqlite_registry):
        class ScriptedLLM:
            def __init__(self):
                self.responses = iter(["bad: [yaml", THREE_DOC])
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return next(self.responses)

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        result = await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert len(llm.calls) == 2  # draft + repair

    async def test_init_with_llm_refuses_when_any_exists(self, kb, sqlite_registry):
        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "schema_notes.yml").write_text("tables: []\n", encoding="utf-8")

        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=THREE_DOC))
        result = await reg.get("kb").handler("init")

        assert "refusing" in result
        assert not (kb.kb_dir / ds / "semantics.yml").exists()
        assert not (kb.kb_dir / ds / "examples.yml").exists()

    async def test_init_llm_parse_failure_after_repair(self, kb, sqlite_registry):
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response="still not yaml"))
        result = await reg.get("kb").handler("init")

        assert "parse" in result.lower()
        ds = sqlite_registry.default_name
        assert not (kb.kb_dir / ds / "schema_notes.yml").exists()

    async def test_init_accepts_merged_single_document(self, kb, sqlite_registry):
        """回归：真实 LLM 常输出单文档三顶层键（无 --- 分隔）。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=MERGED_DOC))
        result = await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        assert "Initialized" in result
        assert (kb.kb_dir / ds / "schema_notes.yml").exists()
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert (kb.kb_dir / ds / "examples.yml").exists()
        assert "学生数" in (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")

    def test_parse_init_docs_accepts_both_formats(self):
        from trove.cli.commands.kb_cmds import _parse_init_docs

        tables, terms, examples = _parse_init_docs(THREE_DOC)
        assert len(tables) == 1 and len(terms) == 1 and len(examples) == 1

        tables, terms, examples = _parse_init_docs(MERGED_DOC)
        assert len(tables) == 1 and len(terms) == 1 and len(examples) == 1
        assert tables[0]["name"] == "students"

    def test_parse_init_docs_tolerates_extra_top_level_key(self):
        from trove.cli.commands.kb_cmds import _parse_init_docs

        doc = MERGED_DOC + "notes: 初稿，人工审阅\n"
        tables, terms, examples = _parse_init_docs(doc)
        assert len(tables) == 1

    async def test_init_chunks_large_schema_and_merges(self, kb, sqlite_registry, monkeypatch):
        """大 schema 分块调用（每块 1 表）→ 多次 LLM 调用 → 结果合并。"""
        from trove.core.types import SchemaInfo, TableInfo, ColumnInfo
        from trove.cli.commands import kb_cmds

        monkeypatch.setattr(kb_cmds, "INIT_CHUNK_TABLES", 1)

        class FakeRegistry:
            default_name = "test_db"

            async def get_schema(self):
                return SchemaInfo(tables=[
                    TableInfo(name="students", columns=[ColumnInfo(name="grade", type="int")]),
                    TableInfo(name="courses", columns=[ColumnInfo(name="title", type="varchar")]),
                ])

        class ScriptedLLM:
            def __init__(self):
                docs = [
                    MERGED_DOC,  # students
                    MERGED_DOC.replace("students", "courses").replace("学生", "课程"),  # courses
                ]
                self.responses = iter(docs)
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append((messages, kwargs))
                return next(self.responses)

        llm = ScriptedLLM()
        reg = SlashRegistry()
        register_kb_commands(reg, {
            "kb": kb,
            "connector_registry": FakeRegistry(),
            "llm_gateway": llm,
            "config": AgentConfig(target="mock/model"),
        })

        result = await reg.get("kb").handler("init")
        assert "Initialized" in result
        assert len(llm.calls) == 2
        # 每块调用都带更大的 max_tokens（防止 4096 截断大 schema）
        assert all(kwargs.get("max_tokens") == 8192 for _, kwargs in llm.calls)

        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "students" in notes and "courses" in notes  # 两块合并
        semantics = (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")
        assert "学生数" in semantics and "课程数" in semantics


class TestKbList:
    async def test_list_empty(self, kb):
        reg = make_reg(kb)
        result = await reg.get("kb").handler("list")
        assert "empty" in result.lower()

    async def test_list_shows_counts_grouped_by_datasource(self, kb, sqlite_registry):
        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "semantics.yml").write_text(
            "terms:\n  - term: 平均成绩\n    mapping: AVG(grade)\n    tables: [students]\n",
            encoding="utf-8",
        )
        reg = make_reg(kb, connector_registry=sqlite_registry)
        result = await reg.get("kb").handler("list")
        assert ds in result
        assert "term: 1" in result


class TestKbReload:
    async def test_reload_purges_deleted_files(self, kb, sqlite_registry):
        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "examples.yml").write_text(
            "examples:\n  - question: q\n    sql: SELECT 1\n    tags: [x]\n",
            encoding="utf-8",
        )
        reg = make_reg(kb, connector_registry=sqlite_registry)
        await reg.get("kb").handler("list")  # syncs

        (kb.kb_dir / ds / "examples.yml").unlink()
        result = await reg.get("kb").handler("reload")
        assert "reloaded" in result
        listed = await reg.get("kb").handler("list")
        assert "empty" in listed.lower()


class TestKbLearn:
    def _session_with_exchange(self):
        session = Session()
        session.messages = [
            Message(role="user", content="学生们的平均成绩是多少"),
            Message(
                role="assistant",
                content="answer",
                metadata={"sql": "SELECT county, AVG(grade) FROM students GROUP BY county"},
            ),
        ]
        return session

    async def test_learn_drafts_and_confirms(self, kb, sqlite_registry):
        llm = LLMGateway(mock_response=DRAFT_YAML)
        reg = make_reg(
            kb,
            llm_gateway=llm,
            current_session=self._session_with_exchange(),
            connector_registry=sqlite_registry,
        )

        result = await reg.get("kb").handler("learn")
        assert "yes" in result  # asks for confirmation
        assert kb.pending_draft is not None
        ds = sqlite_registry.default_name
        assert not (kb.kb_dir / ds / "examples.yml").exists()  # nothing written yet

        result2 = await reg.get("kb").handler("learn --yes")
        assert (kb.kb_dir / ds / "examples.yml").exists()
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert kb.pending_draft is None

        await kb.ensure_synced(ds)
        hits = await kb.search_examples("平均成绩", ds)
        assert any(h.question == "学生们的平均成绩是多少" for h in hits)

    async def test_learn_yes_without_draft(self, kb):
        reg = make_reg(kb)
        result = await reg.get("kb").handler("learn --yes")
        assert "draft" in result.lower()

    async def test_learn_yes_requires_datasource(self, kb):
        """确认写入需要数据源上下文（写入目标目录由数据源名决定）。"""
        kb.pending_draft = {"example": {"question": "q", "sql": "SELECT 1"}, "terms": []}
        reg = make_reg(kb)  # no connector_registry
        result = await reg.get("kb").handler("learn --yes")
        assert "datasource" in result.lower()
        assert kb.pending_draft is not None  # draft kept for retry

    async def test_learn_requires_completed_query(self, kb):
        session = Session()  # no messages
        reg = make_reg(kb, llm_gateway=LLMGateway(mock_response="OK"), current_session=session)
        result = await reg.get("kb").handler("learn")
        assert "question" in result.lower()

    async def test_learn_parse_failure(self, kb):
        llm = LLMGateway(mock_response="not yaml at all")
        reg = make_reg(kb, llm_gateway=llm, current_session=self._session_with_exchange())
        result = await reg.get("kb").handler("learn")
        assert "parse" in result.lower() or "draft" in result.lower()
        assert kb.pending_draft is None

    async def test_learn_strips_markdown_fences(self, kb):
        """LLM 用 ```yaml 围栏输出时也能解析。"""
        fenced = "```yaml\n" + DRAFT_YAML + "\n```"
        reg = make_reg(
            kb,
            llm_gateway=LLMGateway(mock_response=fenced),
            current_session=self._session_with_exchange(),
        )
        result = await reg.get("kb").handler("learn")
        assert kb.pending_draft is not None
        assert kb.pending_draft["example"]["question"] == "学生们的平均成绩是多少"
        assert "yes" in result

    async def test_learn_repairs_broken_draft(self, kb):
        """第一版 YAML 坏（未缩进多行 SQL）→ 自修复轮次用错误反馈让 LLM 重写。"""

        class ScriptedLLM:
            def __init__(self):
                self.responses = iter([
                    """example:
  question: 学生们的平均成绩是多少
  sql: SELECT county, AVG(grade) FROM students
FROM students
GROUP BY county
""",  # 坏：未缩进续行
                    DRAFT_YAML,  # 修复版
                ])
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return next(self.responses)

        llm = ScriptedLLM()
        reg = make_reg(kb, llm_gateway=llm, current_session=self._session_with_exchange())
        result = await reg.get("kb").handler("learn")
        assert kb.pending_draft is not None
        assert kb.pending_draft["example"]["question"] == "学生们的平均成绩是多少"
        assert len(llm.calls) == 2  # draft + repair round
        # repair prompt carried the parse error
        assert "failed to parse" in llm.calls[1][-1]["content"].lower()

    async def test_learn_repair_failure_reports_error(self, kb):
        class ScriptedLLM:
            async def chat(self, model, messages, **kwargs):
                return "still: not yaml"

        reg = make_reg(kb, llm_gateway=ScriptedLLM(), current_session=self._session_with_exchange())
        result = await reg.get("kb").handler("learn")
        assert "repair" in result.lower() or "parse" in result.lower()
        assert kb.pending_draft is None
