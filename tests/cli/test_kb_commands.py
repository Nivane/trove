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

    async def test_init_overwrite_regenerates_existing_files(self, kb, sqlite_registry):
        """--overwrite:重新生成已存在的 KB 文件(旧 flat semantics 迁移路径)。"""
        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "semantics.yml").write_text(
            "terms:\n  - term: 旧术语\n    mapping: COUNT(*)\n", encoding="utf-8")

        llm = LLMGateway(mock_response=TABLES_DOC)
        reg = make_reg(kb, connector_registry=sqlite_registry, llm_gateway=llm,
                       config=AgentConfig(target="mock/model"))
        result = await reg.get("kb").handler("init --overwrite")
        assert "Initialized" in result
        text = (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")
        assert "semantic_model" in text
        assert "COUNT(students.id)" in text


# LLM 只起草表格注释(描述+枚举含义);terms/examples 由代码确定性生成。
# 默认语言为英文(benchmark 均为英文问题)。
TABLES_DOC = """tables:
- name: students
  description: student records
  columns:
  - name: id
    type: int
    description: student identifier
    enums: []
  - name: grade
    type: int
    description: grade
    enums: []
  - name: county
    type: varchar
    description: county
    enums: []
  metrics: []
"""

TABLES_DOC_ZH = """tables:
- name: students
  description: 学生表
  columns:
  - name: id
    type: int
    description: 学生唯一标识符
    enums: []
  - name: grade
    type: int
    description: 成绩
    enums: []
  - name: county
    type: varchar
    description: 所在县
    enums: []
  metrics: []
"""

# 真实 LLM 的自然输出:'---' 分隔多文档,第一份携带 tables
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

# 单文档三顶层键(旧格式容忍:terms/examples 被忽略,仅 tables 生效)
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
        """有 LLM 时 /kb init 生成三份文件:描述由 LLM 起草,terms/examples 确定性生成(默认英文)。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC))
        result = await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        assert "Initialized" in result
        assert (kb.kb_dir / ds / "schema_notes.yml").exists()
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert (kb.kb_dir / ds / "examples.yml").exists()
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "student records" in notes
        # 确定性 term:count + 数值列 SUM/AVG;ID 列不生成;表达式带表限定
        semantics = (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")
        assert "number of students records" in semantics and "average grade" in semantics
        assert "COUNT(students.id)" in semantics
        # 确定性模板:count + 首条文本列 GROUP BY
        examples = (kb.kb_dir / ds / "examples.yml").read_text(encoding="utf-8")
        assert "SELECT COUNT(*) FROM students" in examples
        assert "How many records are in the students table?" in examples
        assert "SELECT county, COUNT(*) FROM students GROUP BY county" in examples

    async def test_init_lang_zh_keeps_chinese_generation(self, kb, sqlite_registry):
        """--lang zh:沿用中文术语/模板与中文起草提示词。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC_ZH))
        result = await reg.get("kb").handler("init --lang zh")

        ds = sqlite_registry.default_name
        assert "Initialized" in result
        semantics = (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")
        assert "学生总数" in semantics and "平均成绩" in semantics

    async def test_init_default_prompt_is_english(self, kb, sqlite_registry):
        """默认起草提示词要求英文描述(枚举含义也英文)。"""
        class ScriptedLLM:
            def __init__(self):
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return TABLES_DOC

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        await reg.get("kb").handler("init")

        system = llm.calls[0][0]["content"]
        assert "in English" in system

    async def test_init_deterministic_terms_skip_undescribed_columns(self, kb, sqlite_registry):
        """不透明列(无描述)不生成 SUM/AVG 术语——防止瞎猜映射。"""
        doc = """tables:
- name: students
  description: 学生表
  columns:
  - name: grade
    type: int
    description: ""
    enums: []
  metrics: []
"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=doc))
        await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        semantics = (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")
        assert "SUM(" not in semantics and "AVG(" not in semantics

    async def test_init_probe_fills_raw_enum_values(self, kb, sqlite_registry):
        """LLM 未写枚举含义的列 → 探测到的原始取值兜底写入 schema_notes。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC))
        await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "Alameda" in notes  # students.county 的 distinct 值

    async def test_init_prompt_shows_sample_values(self, kb, sqlite_registry):
        """低基数文本列的样例值出现在起草提示词里(辅助 LLM 猜含义)。"""
        class ScriptedLLM:
            def __init__(self):
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return TABLES_DOC

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        await reg.get("kb").handler("init")

        user_content = llm.calls[0][-1]["content"]
        assert "Alameda" in user_content  # county 样例值

    async def test_init_prompt_shows_statistics(self, kb, sqlite_registry):
        """统计证据(null/distinct/极值/形状)进入 LLM 起草提示词(AskData 式总结)。"""
        class ScriptedLLM:
            def __init__(self):
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return TABLES_DOC

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        await reg.get("kb").handler("init")

        user_content = llm.calls[0][-1]["content"]
        assert "75..99" in user_content               # grade 数值范围
        assert "5 distinct" in user_content           # id/grade 基数
        assert "3-5 chars" in user_content            # name 长度(列未被 LLM 响应覆盖也进提示词)
        system = llm.calls[0][0]["content"]
        assert "hard evidence" in system              # 统计规则进入 system prompt

    async def test_init_stats_written_to_schema_notes(self, kb, sqlite_registry):
        """profiling 统计与精确行数落盘 schema_notes.yml(stats 字段)。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC))
        await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "row_count: 5" in notes
        assert "stats:" in notes
        assert "distinct: 5" in notes
        assert "max: 99" in notes
        assert "shape: capital" in notes

    async def test_llm_drafted_stats_stripped(self, kb, sqlite_registry):
        """LLM 草稿里的伪造 stats 被剥掉,真实探测统计胜出。"""
        doc = """tables:
- name: students
  description: student records
  row_count: 9999
  columns:
  - name: grade
    type: int
    description: grade
    enums: []
    stats:
      distinct: 999
      null_ratio: 0.5
  metrics: []
"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=doc))
        await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "row_count: 5" in notes      # 真实行数,不是 LLM 的 9999
        assert "9999" not in notes
        assert "distinct: 5" in notes       # 真实 distinct,不是 999
        assert "999" not in notes
        assert "null_ratio: 0.5" not in notes  # grade 无 NULL

    async def test_init_with_docs_imports_official_descriptions(self, kb, sqlite_registry, tmp_path):
        """--docs:官方列描述导入,覆盖 LLM 草稿(枚举含义一并导入)。"""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "students.csv").write_text(
            "original_column_name,column_description,value_description\n"
            'grade,官方成绩说明,"A=优秀"\n',
            encoding="utf-8",
        )

        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC))
        result = await reg.get("kb").handler(f"init --docs {docs_dir}")

        assert "Initialized" in result
        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "官方成绩说明" in notes  # docs 覆盖 LLM 草稿的"成绩"
        assert "A=优秀" in notes

    async def test_init_with_llm_repairs_broken_draft(self, kb, sqlite_registry):
        class ScriptedLLM:
            def __init__(self):
                self.responses = iter(["bad: [yaml", TABLES_DOC])
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return next(self.responses)

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        result = await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert len(llm.calls) >= 2  # draft + repair(合成 few-shot 的额外调用会被静默跳过)

    async def test_init_recovers_draft_from_reasoning_prose(self, kb, sqlite_registry):
        """回归:推理模型 content 为空、全部输出进 reasoning → 网关回退 prose。
        草稿若嵌在 prose 的 ```yaml 围栏里,应直接回收,不触发修复轮。"""
        prose = (
            "Let me think through this schema carefully.\n"
            "The table layout is:\n"
            "```yaml\n" + TABLES_DOC + "```\n"
            "I believe that covers everything.\n"
        )

        class ScriptedLLM:
            def __init__(self):
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                if len(self.calls) > 1:
                    raise AssertionError("unexpected LLM call")  # 修复轮走到 → 测试失败
                return prose

        reg = self._reg(kb, sqlite_registry, ScriptedLLM())
        result = await reg.get("kb").handler("init")

        assert "Initialized" in result
        ds = sqlite_registry.default_name
        assert "student records" in (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")

    async def test_init_rejects_truncated_recovered_draft(self, kb, sqlite_registry):
        """回收草稿缺表(推理被截断)→ 走修复轮;修复轮不再把 prose 回声进上下文。"""
        truncated = (
            "Let me draft the tables.\n"
            "```yaml\ntables:\n"
            "- name: other\n  description: wrong\n  columns: []\n  metrics: []\n"
            "```\n"
        )

        class ScriptedLLM:
            def __init__(self):
                self.calls = []
                self.responses = iter([truncated, TABLES_DOC])

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                try:
                    return next(self.responses)
                except StopIteration:
                    raise AssertionError("unexpected extra LLM call")

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        result = await reg.get("kb").handler("init")

        assert "Initialized" in result
        assert len(llm.calls) == 3  # draft + repair + synthetic(静默跳过)
        repair_messages = llm.calls[1]
        # 修复轮上下文不含 assistant 回声(prose 是噪声,只保留 schema + 错误)
        assert all(m["role"] != "assistant" for m in repair_messages)
        assert "missing" in repair_messages[-1]["content"]

    async def test_init_repairs_only_missing_tables(self, kb, sqlite_registry):
        """回归:部分草稿走外科手术式修复——只补缺表(任务变小,推理 CoT
        更短,预算内更容易产出完整正文),修复结果按表名合并;噪声表剥掉。"""
        truncated = (
            "```yaml\ntables:\n"
            "- name: other\n  description: wrong\n  columns: []\n  metrics: []\n"
            "```\n"
        )

        class ScriptedLLM:
            def __init__(self):
                self.calls = []
                self.responses = iter([truncated, TABLES_DOC])

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                try:
                    return next(self.responses)
                except StopIteration:
                    raise AssertionError("unexpected extra LLM call")

        llm = ScriptedLLM()
        reg = self._reg(kb, sqlite_registry, llm)
        result = await reg.get("kb").handler("init")

        assert "Initialized" in result
        assert len(llm.calls) == 3  # draft + missing-only repair + synthetic(静默跳过)
        repair_messages = llm.calls[1]
        assert all(m["role"] != "assistant" for m in repair_messages)
        content = repair_messages[-1]["content"]
        assert "students" in content  # 只补缺表
        assert "missing" in content
        assert "other" not in content  # 已覆盖表不回显,任务不放大
        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "student records" in notes
        assert "wrong" not in notes  # 噪声表不进 schema_notes

    async def test_init_with_llm_refuses_when_any_exists(self, kb, sqlite_registry):
        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "schema_notes.yml").write_text("tables: []\n", encoding="utf-8")

        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC))
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
        """回归：旧格式单文档三顶层键仍可解析(terms/examples 被忽略)。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=MERGED_DOC))
        result = await reg.get("kb").handler("init")

        ds = sqlite_registry.default_name
        assert "Initialized" in result
        assert (kb.kb_dir / ds / "schema_notes.yml").exists()
        assert (kb.kb_dir / ds / "semantics.yml").exists()
        assert (kb.kb_dir / ds / "examples.yml").exists()
        assert "number of students records" in (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")

    def test_parse_init_tables_accepts_both_formats(self):
        from trove.cli.commands.kb_cmds import _parse_init_tables

        tables = _parse_init_tables(THREE_DOC)
        assert len(tables) == 1
        assert tables[0]["name"] == "students"

        tables = _parse_init_tables(MERGED_DOC)
        assert len(tables) == 1
        assert tables[0]["name"] == "students"

    def test_recover_init_tables_from_fenced_prose(self):
        from trove.cli.commands.kb_cmds import _recover_init_tables

        prose = "Let me think.\n```yaml\n" + TABLES_DOC + "```\nDone.\n"
        tables = _recover_init_tables(prose)
        assert tables is not None
        assert tables[0]["name"] == "students"

    def test_recover_init_tables_from_tables_rooted_prose(self):
        from trove.cli.commands.kb_cmds import _recover_init_tables

        prose = "I will now write the draft.\n\n" + TABLES_DOC
        tables = _recover_init_tables(prose)
        assert tables is not None
        assert tables[0]["name"] == "students"

    def test_recover_init_tables_none_for_pure_prose(self):
        from trove.cli.commands.kb_cmds import _recover_init_tables

        assert _recover_init_tables(
            "Just thinking about the schema, no draft written yet."
        ) is None

    def test_parse_init_tables_tolerates_extra_top_level_key(self):
        from trove.cli.commands.kb_cmds import _parse_init_tables

        doc = TABLES_DOC + "notes: 初稿，人工审阅\n"
        tables = _parse_init_tables(doc)
        assert len(tables) == 1

    def test_parse_init_tables_rejects_missing_section(self):
        from trove.cli.commands.kb_cmds import _parse_init_tables

        with pytest.raises(ValueError):
            _parse_init_tables("terms:\n- term: x\n")

    def test_lang_arg_defaults_to_english(self):
        from trove.cli.commands.kb_cmds import _lang_arg

        assert _lang_arg("init") == "en"
        assert _lang_arg("init --docs /tmp/x") == "en"
        assert _lang_arg("init --lang zh") == "zh"
        assert _lang_arg("init --lang zh --docs /tmp/x") == "zh"

    async def test_init_chunks_large_schema_and_merges(self, kb, sqlite_registry, monkeypatch):
        """大 schema 分块调用（每块 1 表）→ 多次 LLM 调用 → 结果合并。"""
        from trove.core.types import SchemaInfo, TableInfo, ColumnInfo
        from trove.cli.commands import kb_cmds

        monkeypatch.setattr(
            "trove.services.kb.init_pipeline.INIT_CHUNK_TABLES", 1)

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
                    TABLES_DOC,  # students
                    TABLES_DOC.replace("students", "courses").replace("学生", "课程"),  # courses
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
        assert len(llm.calls) >= 2  # 每块 1 次起草;合成 few-shot 的额外调用会被静默跳过
        # 每块调用都带更大的 max_tokens（防止 4096 截断大 schema;
        # 推理模型的 max_tokens 计入 CoT,预算要给 CoT+草稿留余量)
        assert all(
            kwargs.get("max_tokens") == kb_cmds.INIT_MAX_TOKENS
            for _, kwargs in llm.calls
        )

        ds = sqlite_registry.default_name
        notes = (kb.kb_dir / ds / "schema_notes.yml").read_text(encoding="utf-8")
        assert "students" in notes and "courses" in notes  # 两块合并
        semantics = (kb.kb_dir / ds / "semantics.yml").read_text(encoding="utf-8")
        assert "number of students records" in semantics and "number of courses records" in semantics  # 确定性生成


class TestKbList:
    async def test_list_empty(self, kb):
        reg = make_reg(kb)
        result = await reg.get("kb").handler("list")
        assert "empty" in result.lower()

    async def test_list_shows_counts_grouped_by_datasource(self, kb, sqlite_registry):
        from tests.helpers.kb import ossie_semantics_yaml

        ds = sqlite_registry.default_name
        (kb.kb_dir / ds).mkdir(parents=True)
        (kb.kb_dir / ds / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "平均成绩", "mapping": "AVG(students.grade)", "tables": ["students"]},
        ]))
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


SYNTH_JSON = """{"examples": [
  {"question": "Average grade by county",
   "sql": "SELECT county, AVG(grade) FROM students GROUP BY county",
   "tags": ["students", "aggregation"]},
  {"question": "Top student by grade",
   "sql": "SELECT name, grade FROM students ORDER BY grade DESC LIMIT 1",
   "tags": ["students", "order"]}
]}"""


class TestKbInitSynthetic:
    """/kb init 的合成 few-shot:LLM 生成 Q/SQL 对,经双护栏并入 examples.yml。"""

    def _reg(self, kb, sqlite_registry, llm):
        reg = SlashRegistry()
        register_kb_commands(reg, {
            "kb": kb,
            "connector_registry": sqlite_registry,
            "llm_gateway": llm,
            "config": AgentConfig(target="mock/model"),
        })
        return reg

    async def test_init_appends_synthetic_examples(self, kb, sqlite_registry):
        """起草响应后跟合成 JSON:examples.yml 同时含确定性模板与合成条目。"""
        class ScriptedLLM:
            def __init__(self):
                self._responses = iter([TABLES_DOC, SYNTH_JSON])

            async def chat(self, model, messages, **kwargs):
                return next(self._responses)

        reg = self._reg(kb, sqlite_registry, ScriptedLLM())
        result = await reg.get("kb").handler("init")
        assert "Initialized" in result

        ds = sqlite_registry.default_name
        examples = (kb.kb_dir / ds / "examples.yml").read_text(encoding="utf-8")
        assert "How many records are in the students table?" in examples  # 确定性模板
        assert "Average grade by county" in examples                     # 合成条目
        assert "SELECT county, AVG(grade) FROM students GROUP BY county" in examples

    async def test_init_synthetic_rejects_unrunnable_sql(self, kb, sqlite_registry):
        """护栏丢弃跑不起来的 SQL(未知列),init 不中断。"""
        class ScriptedLLM:
            def __init__(self):
                self._responses = iter([TABLES_DOC, """{"examples": [
                  {"question": "broken", "sql": "SELECT nope FROM students", "tags": ["students"]}
                ]}"""])

            async def chat(self, model, messages, **kwargs):
                return next(self._responses)

        reg = self._reg(kb, sqlite_registry, ScriptedLLM())
        result = await reg.get("kb").handler("init")
        assert "Initialized" in result

        ds = sqlite_registry.default_name
        examples = (kb.kb_dir / ds / "examples.yml").read_text(encoding="utf-8")
        assert "broken" not in examples

    async def test_init_synthetic_garbage_keeps_deterministic_templates(
        self, kb, sqlite_registry,
    ):
        """LLM 合成响应不可解析:静默跳过,确定性模板保留。"""
        reg = self._reg(kb, sqlite_registry, LLMGateway(mock_response=TABLES_DOC))
        result = await reg.get("kb").handler("init")
        assert "Initialized" in result

        ds = sqlite_registry.default_name
        examples = (kb.kb_dir / ds / "examples.yml").read_text(encoding="utf-8")
        assert "How many records are in the students table?" in examples


class TestKbLessons:
    async def test_lessons_list_and_confirm(self, kb, sqlite_registry):
        reg = make_reg(kb, connector_registry=sqlite_registry)
        ds = sqlite_registry.default_name
        await kb.append_lesson({"pattern": "p1", "note": "n", "sql_snippet": "s"}, ds)

        listed = await reg.get("kb").handler("lessons")
        assert "1 pending" in listed
        assert "p1" in listed

        confirmed = await reg.get("kb").handler("lessons --yes")
        assert "1 lesson" in confirmed
        assert len(await kb.list_lessons(ds)) == 1


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

    async def test_learn_recovers_draft_from_reasoning_prose(self, kb):
        """推理模型 content 为空 → 网关回退 prose;围栏内草稿直接回收,不触发修复轮。"""
        prose = (
            "Let me think about this question.\n"
            "The example should be:\n"
            "```yaml\n" + DRAFT_YAML + "```\n"
            "Yes, that is correct.\n"
        )

        class ScriptedLLM:
            def __init__(self):
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                if len(self.calls) > 1:
                    raise AssertionError("unexpected LLM call")  # 修复轮走到 → 测试失败
                return prose

        reg = make_reg(
            kb, llm_gateway=ScriptedLLM(), current_session=self._session_with_exchange(),
        )
        result = await reg.get("kb").handler("learn")
        assert kb.pending_draft is not None
        assert kb.pending_draft["example"]["question"] == "学生们的平均成绩是多少"
        assert "yes" in result

    async def test_learn_repair_failure_reports_error(self, kb):
        class ScriptedLLM:
            async def chat(self, model, messages, **kwargs):
                return "still: not yaml"

        reg = make_reg(kb, llm_gateway=ScriptedLLM(), current_session=self._session_with_exchange())
        result = await reg.get("kb").handler("learn")
        assert "repair" in result.lower() or "parse" in result.lower()
        assert kb.pending_draft is None
