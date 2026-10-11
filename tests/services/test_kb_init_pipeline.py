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


SYNTH_JSON = """{"examples": [
    {"question": "How many records in students?", "sql": "SELECT COUNT(*) FROM students"},
    {"question": "What is the average grade?", "sql": "SELECT AVG(grade) FROM students"}
]}"""

DRAFT_ANNOTATIONS = """
annotations:
  - table: students
    field_notes:
      - name: county
        synonyms: [district, region]
        description: county name
      - name: grade
        synonyms: [score]
"""


async def test_init_kb_semantic_draft_reaches_single_source(tmp_path, sqlite_registry):
    """P4:起草步骤把字段 synonyms 合并进语义模型单一真源(semantics.yml),
    SemanticLayerProvider(KB semantics 路径)可达。"""
    class QueueLLM:
        def __init__(self, *responses):
            self._q = list(responses)

        async def chat(self, model, messages, **kwargs):
            return self._q.pop(0) if self._q else ""

    kb = KbService(tmp_path)
    llm = QueueLLM(TABLES_DOC, SYNTH_JSON, DRAFT_ANNOTATIONS)
    config = AgentConfig(target="mock/model")
    summary = await init_kb(kb, sqlite_registry, llm, config, datasource="test_db", lang="en")
    assert "Initialized" in summary

    from trove.services.semantic_layer.provider import SemanticLayerProvider
    provider = SemanticLayerProvider(
        tmp_path / "empty_semantic", "test_db",
        kb_semantics_path=kb.semantics_path("test_db"),
    )
    model = provider.model()
    assert model is not None
    students = next(d for d in model.datasets if d.name == "students")
    county = next(f for f in students.fields if f.name == "county")
    assert county.synonyms == ["district", "region"]
    assert county.description == "county name"


#: 一张 30 个不同取值的文本表:枚举档(≤20)看不见,第二档(≤100)才收录 ——
#: 正是 ``values`` 存在的理由。
_LABELS_DOC = """tables:
- name: labels
  description: label records
  columns:
  - name: id
    type: int
    description: label identifier
    enums: []
  - name: tag
    type: varchar
    description: tag
    enums: []
  metrics: []
"""


async def test_init_kb_backfills_probed_values(tmp_path):
    """值探测回填接线:init 产出的语义模型带字段级 ``values``(结构事实)。

    labels.tag 有 30 个取值 —— 枚举档(≤20)跳过、第二档(≤100)收录;
    id 是主键、不是文本列 → 不探不填。走真探测(内存 sqlite),不是 mock。
    """
    from trove.core.types import DatasourceConfig
    from trove.services.datasource.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    adapter = await registry.register(
        DatasourceConfig(name="labels_db", type="sqlite",
                         connection_params={"path": ":memory:"}, default=True),
        set_default=True,
    )
    await adapter.execute("CREATE TABLE labels (id INTEGER PRIMARY KEY, tag TEXT)")
    await adapter.execute(
        "INSERT INTO labels (tag) VALUES "
        + ", ".join(f"('t{i:02d}')" for i in range(30))
    )
    try:
        kb = KbService(tmp_path)
        llm = LLMGateway(mock_response=_LABELS_DOC)
        config = AgentConfig(target="mock/model")
        await init_kb(kb, registry, llm, config, datasource="labels_db", lang="en")
    finally:
        await registry.close_all()

    import yaml
    text = (kb.kb_dir / "labels_db" / "semantics.yml").read_text(encoding="utf-8")
    model = yaml.safe_load(text)["semantic_model"][0]
    labels = next(d for d in model["datasets"] if d["name"] == "labels")
    fields = {f["name"]: f for f in labels["fields"]}
    assert fields["tag"]["values"] == [f"t{i:02d}" for i in range(30)]
    assert "values" not in fields["id"]      # 主键/数值列不是取值词表
    assert "enum_display" not in fields["tag"]  # 30 个取值进不了枚举档

    # 装载侧同一份字节:values 是数据,不改角色
    from trove.services.semantic_layer.ossie import parse_ossie
    parsed = parse_ossie(text, preferred_dialect="sqlite")
    tag = next(
        f for d in parsed.datasets if d.name == "labels"
        for f in d.fields if f.name == "tag"
    )
    assert tag.values == [f"t{i:02d}" for i in range(30)]
    assert tag.semantic_role != "enum"


# ── E2:kb init 后置 —— 实体命名草稿 ────────────────────────────────────────
#
# 四条纪律(见 init_pipeline._draft_entity_naming):只落 pending / 读最终
# 模型 / 全状态幂等 / 空声明不起草。命名内容全部由确定性规则复现
# (display_name ← 描述标题词、aliases ← terms 计数词干、natural_keys ←
# 主键)—— KB 反作弊在此不适用:确定性规则能复现的内容就是真内容。


def _entity_drafts(kb, datasource="test_db"):
    from trove.services.semantic_layer.manage import SemanticManager

    return SemanticManager(kb).drafts(datasource)


async def test_init_kb_drafts_entity_naming(tmp_path, sqlite_registry):
    """zh init 后:每个数据集一条 pending 实体命名草稿,内容确定性可复现。

    ``students`` 的描述「学生表」→ 显示名「学生」;terms 的表级计数词
    (学生总数/数量/记录数)→ 别名「学生」;primary_key → 自然键 [id]。
    """
    from tests.cli.test_kb_commands import TABLES_DOC_ZH

    kb = KbService(tmp_path)
    llm = LLMGateway(mock_response=TABLES_DOC_ZH)
    config = AgentConfig(target="mock/model")
    await init_kb(kb, sqlite_registry, llm, config, datasource="test_db", lang="zh")

    drafts = _entity_drafts(kb)
    entity = [d for d in drafts["pending"] if d["kind"] == "entity"]
    assert [d["name"] for d in entity] == ["students"]
    assert entity[0]["payload"] == {
        "dataset": "students",
        "display_name": "学生",
        "aliases": ["学生"],
        "natural_keys": ["id"],
    }
    assert entity[0]["note"].startswith("auto:entity_naming:")

    # 只落 pending:单一真源(模型)里读不到任何实体声明
    import yaml
    model = yaml.safe_load(
        (kb.kb_dir / "test_db" / "semantics.yml").read_text(encoding="utf-8")
    )["semantic_model"][0]
    students = next(d for d in model["datasets"] if d["name"] == "students")
    assert "entity" not in students

    # 草稿文件是独立的一本账
    assert (kb.kb_dir / "test_db" / "semantic_drafts.yml").exists()


async def test_entity_naming_rerun_is_idempotent(tmp_path, sqlite_registry):
    """重跑 init(overwrite 三方合并)不刷屏:pending 仍恰好一条。"""
    from tests.cli.test_kb_commands import TABLES_DOC_ZH

    kb = KbService(tmp_path)
    llm = LLMGateway(mock_response=TABLES_DOC_ZH)
    config = AgentConfig(target="mock/model")
    await init_kb(kb, sqlite_registry, llm, config, datasource="test_db", lang="zh")
    await init_kb(kb, sqlite_registry, llm, config, datasource="test_db", lang="zh",
                  overwrite=True)

    entity = [d for d in _entity_drafts(kb)["pending"] if d["kind"] == "entity"]
    assert [d["name"] for d in entity] == ["students"]


async def test_entity_naming_en_kb_lands_natural_keys_only(tmp_path, sqlite_registry):
    """英文库的诚实边界:显示名两道闸(CJK/异于表名)与别名闸(词干=表名
    排除)都拦下 → 草稿只带 natural_keys —— 表名/主键本就是既有的隐式
    事实,写进声明显式化,不发明任何英文字面量。"""
    kb = KbService(tmp_path)
    llm = LLMGateway(mock_response=TABLES_DOC)
    config = AgentConfig(target="mock/model")
    await init_kb(kb, sqlite_registry, llm, config, datasource="test_db", lang="en")

    entity = [d for d in _entity_drafts(kb)["pending"] if d["kind"] == "entity"]
    assert [d["name"] for d in entity] == ["students"]
    assert entity[0]["payload"] == {"dataset": "students", "natural_keys": ["id"]}


async def test_entity_naming_never_redrafts_rejected(tmp_path, sqlite_registry):
    """拒绝过的命名不回灌(全状态幂等):reject 后手工再跑一遍后置起草 → 0。"""
    from tests.cli.test_kb_commands import TABLES_DOC_ZH
    from trove.services.kb.init_pipeline import _draft_entity_naming
    from trove.services.semantic_layer.manage import SemanticManager

    kb = KbService(tmp_path)
    llm = LLMGateway(mock_response=TABLES_DOC_ZH)
    config = AgentConfig(target="mock/model")
    await init_kb(kb, sqlite_registry, llm, config, datasource="test_db", lang="zh")
    manager = SemanticManager(kb)
    draft_id = next(d["id"] for d in _entity_drafts(kb)["pending"]
                    if d["kind"] == "entity")
    await manager.reject_draft("test_db", draft_id)

    created = await _draft_entity_naming(kb, "test_db", [], [])
    assert created == 0
    assert [d for d in _entity_drafts(kb)["pending"] if d["kind"] == "entity"] == []


async def test_entity_naming_skips_declared_and_empty(tmp_path, sqlite_registry):
    """已声明实体的数据集不再起草;三键全空(无描述词/无别名/无主键)也
    不起草 —— 一条确认了也什么都没声明的草稿是纯噪声。"""
    import yaml
    from trove.services.kb.init_pipeline import _draft_entity_naming

    kb = KbService(tmp_path / "proj")
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True)
    ds_dir.joinpath("semantics.yml").write_text(
        yaml.safe_dump({
            "version": "0.2.0.dev0",
            "semantic_model": [{
                "name": "demo",
                "datasets": [
                    {"name": "loan", "source": "loan", "primary_key": ["id"],
                     "entity": {"display_name": "贷款"},
                     "fields": [{"name": "id", "datatype": "Integer",
                                 "expression": {"dialects": [{
                                     "dialect": "ANSI_SQL",
                                     "expression": "id"}]}}]},
                    {"name": "widget", "source": "widget",
                     "fields": [{"name": "a", "datatype": "String",
                                 "expression": {"dialects": [{
                                     "dialect": "ANSI_SQL",
                                     "expression": "a"}]}}]},
                ],
                "metrics": [],
            }],
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    created = await _draft_entity_naming(kb, "demo", [], [])
    assert created == 0
