"""schema_linking re-injects indexed schema_doc metadata at query time.

Covers the (3) requirement: physical schema metadata (table/column descriptions
+ enum values), indexed into the unified PG retrieval store as `schema_doc`,
is recalled during schema linking and appended to the semantic context so the
planner can anchor columns/enum values. Only the pg_hybrid backend supplies
these docs; other backends return [] and the context is untouched.
"""

import pytest

from trove.services.retrieval.store import RetrievalHit
from trove.workflow.nodes.schema_linking import _semantic_linking


class _Field:
    def __init__(self, name, role="dimension", synonyms=None, enum_display=None):
        self.name = name
        self.semantic_role = role
        self.synonyms = synonyms or []
        self.is_time = role == "time"
        self.enum_display = enum_display or {}


class _Dataset:
    def __init__(self, name, description="", synonyms=None, fields=None):
        self.name = name
        self.description = description
        self.synonyms = synonyms or []
        self.fields = fields or []


class _Metric:
    def __init__(self, name, expression, datasets=None, definition=""):
        self.name = name
        self.expression = expression
        self.datasets = datasets or []
        self.definition = definition


class _Model:
    def __init__(self, datasets, metrics=None, instructions=""):
        self.datasets = datasets
        self.metrics = metrics or []
        self.instructions = instructions


class _SemanticLayer:
    enabled = True

    def model(self):
        return _Model(
            [_Dataset("card", description="客户卡", fields=[
                _Field("type", role="dimension", enum_display={"1": "借记卡"}),
            ])],
            metrics=[_Metric("avg_loan", "AVG(loan.amount)", datasets=["card"])],
        )

    def terms_for(self, query):
        return []

    def field_hits(self, question, matched):
        return []


class _FakeKb:
    def __init__(self, docs):
        self._docs = docs

    async def search_schema_docs(self, query, datasource, limit=5):
        return self._docs[:limit]


def _state(question, datasource="ds"):
    class _S:
        error_analysis = ""
        error = None
    s = _S()
    s.question = question
    s.datasource = datasource
    return s


async def test_schema_doc_injected_into_context():
    kb = _FakeKb([
        RetrievalHit(doc_id="schema:card", content="CARD 表记录客户卡", score=0.9, kind="schema_doc"),
    ])
    state = _state("card 平均贷款")
    base = await _semantic_linking(
        state, kb, None, _SemanticLayer(), [], "card 平均贷款", "ds")
    assert "Retrieved schema notes" in base["semantic_context"]
    assert "CARD 表记录客户卡" in base["semantic_context"]
    assert base["link_detail"]["schema_doc_hits"] == 1
    # schema_context mirrors semantic_context
    assert base["schema_context"] == base["semantic_context"]


async def test_schema_doc_for_unmatched_table_is_filtered_out():
    # doc is for a table not in the matched set → must not be injected
    kb = _FakeKb([
        RetrievalHit(doc_id="schema:other", content="OTHER 表", score=0.5, kind="schema_doc"),
    ])
    state = _state("card 平均贷款")
    base = await _semantic_linking(
        state, kb, None, _SemanticLayer(), [], "card 平均贷款", "ds")
    assert "Retrieved schema notes" not in base["semantic_context"]
    assert "OTHER 表" not in base["semantic_context"]


async def test_no_schema_doc_backend_keeps_context_untouched():
    class _NoSchemaKb:
        async def search_schema_docs(self, query, datasource, limit=5):
            return []

    state = _state("card 平均贷款")
    base = await _semantic_linking(
        state, _NoSchemaKb(), None, _SemanticLayer(), [], "card 平均贷款", "ds")
    assert "Retrieved schema notes" not in base["semantic_context"]
