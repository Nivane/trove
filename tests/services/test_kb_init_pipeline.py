"""init_pipeline tests: LLM-assisted KB init, shared by REPL /kb init
and the admin API (behavior kept identical to the former CLI pipeline).
"""

import pytest

from trove.core.config import AgentConfig
from trove.core.errors import DatasourceError
from trove.llm.gateway import LLMGateway
from trove.services.kb.init_pipeline import init_kb
from trove.services.kb.service import KbService
from tests.cli.test_kb_commands import TABLES_DOC


async def test_init_kb_with_llm_creates_files(tmp_path, sqlite_registry):
    """有 LLM:三份文件生成,描述由 LLM 起草,terms/examples 确定性生成。"""
    kb = KbService(tmp_path)
    llm = LLMGateway(mock_response=TABLES_DOC)
    config = AgentConfig(target="mock/model")
    summary = await init_kb(
        kb, sqlite_registry, llm, config, datasource="test_db", lang="en",
    )
    assert "Initialized" in summary
    for name in ("schema_notes.yml", "semantics.yml", "examples.yml"):
        assert (kb.kb_dir / "test_db" / name).exists()
    notes = (kb.kb_dir / "test_db" / "schema_notes.yml").read_text(encoding="utf-8")
    assert "student records" in notes
    # 确定性 term:count + 数值列 SUM/AVG;ID 列不生成;表达式带表限定
    semantics = (kb.kb_dir / "test_db" / "semantics.yml").read_text(encoding="utf-8")
    assert "number of students records" in semantics and "average grade" in semantics
    assert "COUNT(students.id)" in semantics
    # 结构层:datasets 带 primary_key + fields(含 datatype);关系命名推断
    import yaml
    model = yaml.safe_load(semantics)["semantic_model"][0]
    students = next(d for d in model["datasets"] if d["name"] == "students")
    assert students["primary_key"] == ["id"]
    assert {f["name"] for f in students["fields"]} >= {"id", "county", "grade"}
    # 确定性模板:count + 首条文本列 GROUP BY
    examples = (kb.kb_dir / "test_db" / "examples.yml").read_text(encoding="utf-8")
    assert "SELECT COUNT(*) FROM students" in examples
    assert "How many records are in the students table?" in examples
    assert "SELECT county, COUNT(*) FROM students GROUP BY county" in examples


async def test_init_kb_no_llm_skeleton(tmp_path, sqlite_registry):
    """无 LLM:纯骨架(schema_notes.yml,无描述)。"""
    kb = KbService(tmp_path)
    summary = await init_kb(kb, sqlite_registry, llm=None, config=None,
                            datasource="test_db", lang="en")
    assert "skeleton" in summary
    assert (kb.kb_dir / "test_db" / "schema_notes.yml").exists()


async def test_init_kb_refuses_without_overwrite(tmp_path, sqlite_registry):
    """重复 init 且非 overwrite:抛 DatasourceError。"""
    kb = KbService(tmp_path)
    await init_kb(kb, sqlite_registry, llm=None, config=None,
                  datasource="test_db", lang="en")
    with pytest.raises(DatasourceError):
        await init_kb(kb, sqlite_registry, llm=None, config=None,
                      datasource="test_db", lang="en")
