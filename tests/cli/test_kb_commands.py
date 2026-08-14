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
        assert (kb.kb_dir / "schema_notes.yml").exists()
        assert "students" in (kb.kb_dir / "schema_notes.yml").read_text(encoding="utf-8")

    async def test_init_refuses_overwrite(self, kb, sqlite_registry):
        (kb.kb_dir / "schema_notes.yml").write_text("tables: []\n", encoding="utf-8")
        reg = make_reg(kb, connector_registry=sqlite_registry)
        result = await reg.get("kb").handler("init")
        assert "refusing" in result
        assert (kb.kb_dir / "schema_notes.yml").read_text(encoding="utf-8") == "tables: []\n"


class TestKbList:
    async def test_list_empty(self, kb):
        reg = make_reg(kb)
        result = await reg.get("kb").handler("list")
        assert "empty" in result.lower()

    async def test_list_shows_counts(self, kb):
        (kb.kb_dir / "semantics.yml").write_text(
            "terms:\n  - term: 平均成绩\n    mapping: AVG(grade)\n    tables: [students]\n",
            encoding="utf-8",
        )
        reg = make_reg(kb)
        result = await reg.get("kb").handler("list")
        assert "term: 1" in result


class TestKbReload:
    async def test_reload_purges_deleted_files(self, kb):
        (kb.kb_dir / "examples.yml").write_text(
            "examples:\n  - question: q\n    sql: SELECT 1\n    tags: [x]\n",
            encoding="utf-8",
        )
        reg = make_reg(kb)
        await reg.get("kb").handler("list")  # syncs

        (kb.kb_dir / "examples.yml").unlink()
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

    async def test_learn_drafts_and_confirms(self, kb):
        llm = LLMGateway(mock_response=DRAFT_YAML)
        reg = make_reg(kb, llm_gateway=llm, current_session=self._session_with_exchange())

        result = await reg.get("kb").handler("learn")
        assert "yes" in result  # asks for confirmation
        assert kb.pending_draft is not None
        assert not (kb.kb_dir / "examples.yml").exists()  # nothing written yet

        result2 = await reg.get("kb").handler("learn --yes")
        assert (kb.kb_dir / "examples.yml").exists()
        assert (kb.kb_dir / "semantics.yml").exists()
        assert kb.pending_draft is None

        await kb.ensure_synced()
        hits = await kb.search_examples("平均成绩")
        assert any(h.question == "学生们的平均成绩是多少" for h in hits)

    async def test_learn_yes_without_draft(self, kb):
        reg = make_reg(kb)
        result = await reg.get("kb").handler("learn --yes")
        assert "draft" in result.lower()

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
