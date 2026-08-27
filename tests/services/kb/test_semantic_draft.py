"""semantic_draft tests: LLM 语义层起草 + 白名单校验。

LLM 只补 synonyms/description;任何新增列/表/键/表达式被消费端剥掉
(白名单),解析失败保持原结构(增强是锦上添花)。
"""

import pytest

from trove.services.kb.semantic_draft import (
    apply_annotations,
    draft_semantic_annotations,
)


def _doc():
    return {
        "semantic_model": [{
            "name": "fin",
            "datasets": [
                {"name": "district", "source": "district", "primary_key": [],
                 "fields": [
                     {"name": "A3", "expression": {"dialects": [
                         {"dialect": "ANSI_SQL", "expression": "A3"}]},
                      "datatype": "String"},
                     {"name": "A11", "expression": {"dialects": [
                         {"dialect": "ANSI_SQL", "expression": "A11"}]},
                      "datatype": "Decimal"},
                 ]},
                {"name": "loan", "source": "loan", "primary_key": [],
                 "fields": [
                     {"name": "amount", "expression": {"dialects": [
                         {"dialect": "ANSI_SQL", "expression": "amount"}]},
                      "datatype": "Decimal"},
                 ]},
            ],
            "relationships": [],
        }],
    }


def _field(model, dataset, name):
    d = next(x for x in model["datasets"] if x["name"] == dataset)
    return next(f for f in d["fields"] if f["name"] == name)


class TestApplyAnnotations:
    def test_applies_synonyms_and_description(self):
        doc = _doc()
        model = doc["semantic_model"][0]
        applied, dropped = apply_annotations(model, [{
            "table": "district",
            "field_notes": [
                {"name": "A3", "synonyms": ["region", "area"],
                 "description": "district name"},
            ],
        }])
        assert (applied, dropped) == (1, 0)
        a3 = _field(model, "district", "A3")
        assert a3["ai_context"]["synonyms"] == ["region", "area"]
        assert a3["description"] == "district name"

    def test_unknown_field_dropped(self):
        doc = _doc()
        model = doc["semantic_model"][0]
        applied, dropped = apply_annotations(model, [{
            "table": "loan",
            "field_notes": [{"name": "ghost", "synonyms": ["x"], "description": "y"}],
        }])
        assert (applied, dropped) == (0, 1)
        assert all(f["name"] != "ghost" for f in model["datasets"][1]["fields"])

    def test_unknown_table_dropped(self):
        doc = _doc()
        model = doc["semantic_model"][0]
        applied, dropped = apply_annotations(model, [{
            "table": "planet",
            "field_notes": [{"name": "A3", "synonyms": ["region"], "description": "z"}],
        }])
        assert (applied, dropped) == (0, 1)

    def test_junk_keys_and_new_fields_stripped(self):
        """LLM 越界输出(新增列/改 type/加表达式)→ 白名单只留 aliases+desc。"""
        doc = _doc()
        model = doc["semantic_model"][0]
        applied, dropped = apply_annotations(model, [{
            "table": "loan",
            "field_notes": [
                {"name": "amount", "synonyms": ["loan value"],
                 "description": "d", "expression": "SUM(x)", "datatype": "String",
                 "not_a_key": True},
                {"name": "brand_new_column", "synonyms": ["n"], "description": "m"},
            ],
        }])
        assert applied == 1
        assert dropped == 1  # brand_new_column 不在声明字段 → 剥掉
        assert "brand_new_column" not in {
            f["name"] for f in model["datasets"][1]["fields"]}
        amount = _field(model, "loan", "amount")
        assert amount["datatype"] == "Decimal"       # 未被越界覆盖
        # 越界 expression 未被写入:仍是结构层表达式
        assert amount["expression"]["dialects"][0]["expression"] == "amount"
        assert amount["ai_context"]["synonyms"] == ["loan value"]

    def test_empty_entry_dropped(self):
        doc = _doc()
        model = doc["semantic_model"][0]
        applied, dropped = apply_annotations(model, [{
            "table": "loan",
            "field_notes": [{"name": "amount", "synonyms": [], "description": ""}],
        }])
        assert (applied, dropped) == (0, 1)


class TestEnumLabels:
    """enum_labels:LLM 只补已声明 code 的可读词;未声明 code 丢弃(白名单)。"""

    def test_applies_labels_to_declared_codes(self):
        doc = _doc()
        model = doc["semantic_model"][0]
        model["datasets"][1]["fields"].append({
            "name": "gender", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "gender"}]},
            "datatype": "String", "semantic_role": "enum",
            "enum_display": {"F": "F", "M": "M"},
        })
        applied, dropped = apply_annotations(model, [{
            "table": "loan",
            "field_notes": [{"name": "gender", "synonyms": ["sex"],
                             "description": "client gender"}],
            "enum_labels": {"gender": {"F": "female", "M": "male"}},
        }])
        assert applied == 2
        assert dropped == 0
        gender = _field(model, "loan", "gender")
        assert gender["enum_display"] == {"F": "female", "M": "male"}
        assert gender["ai_context"]["synonyms"] == ["sex"]

    def test_undeclared_code_ignored(self):
        """LLM 发明枚举 code → 丢弃,不污染 enum_display。"""
        doc = _doc()
        model = doc["semantic_model"][0]
        model["datasets"][1]["fields"].append({
            "name": "gender", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "gender"}]},
            "datatype": "String", "semantic_role": "enum",
            "enum_display": {"F": "F", "M": "M"},
        })
        applied, dropped = apply_annotations(model, [{
            "table": "loan",
            "field_notes": [{"name": "gender", "synonyms": ["sex"],
                             "description": "client gender"}],
            "enum_labels": {"gender": {"F": "female", "X": "unknown"}},
        }])
        gender = _field(model, "loan", "gender")
        assert gender["enum_display"] == {"F": "female", "M": "M"}  # X 被剥掉
        assert gender["ai_context"]["synonyms"] == ["sex"]


class ScriptedLLM:
    def __init__(self, *responses):
        self._q = list(responses)
        self.calls = 0

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        if not self._q:
            return ""
        return self._q.pop(0)


DRAFT = """
annotations:
  - table: district
    field_notes:
      - name: A3
        synonyms: [region, area]
        description: district name
      - name: A11
        synonyms: [salary]
        description: average salary level
"""


class TestDraftPipeline:
    async def test_enriches_structure_in_chunks(self):
        llm = ScriptedLLM(DRAFT)
        doc = await draft_semantic_annotations(
            llm, "mock/model", _doc(), chunk_size=1)
        model = doc["semantic_model"][0]
        a3 = _field(model, "district", "A3")
        assert a3["ai_context"]["synonyms"] == ["region", "area"]
        # loan 块(第二块)无响应 → 保持原结构,不报错
        assert all(f["name"] != "ghost" for f in model["datasets"][1]["fields"])

    async def test_unparseable_response_keeps_structure(self):
        llm = ScriptedLLM("not yaml at all [[[")
        doc = await draft_semantic_annotations(llm, "mock/model", _doc())
        assert all("ai_context" not in f for f in doc["semantic_model"][0]["datasets"][0]["fields"])

    async def test_recovers_from_fenced_prose(self):
        llm = ScriptedLLM("Here is my reasoning...\n```yaml\n" + DRAFT + "\n```")
        doc = await draft_semantic_annotations(llm, "mock/model", _doc())
        assert _field(doc["semantic_model"][0], "district", "A3")["ai_context"]["synonyms"] == [
            "region", "area"]

    async def test_chat_failure_skipped(self):
        class BoomLLM:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("boom")

        doc = await draft_semantic_annotations(BoomLLM(), "mock/model", _doc())
        assert all("ai_context" not in f for f in doc["semantic_model"][0]["datasets"][0]["fields"])