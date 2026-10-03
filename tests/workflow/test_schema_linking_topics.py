"""主题域在 schema linking 主路径上的收敛测试。

「带 topic 时范围确实收敛」在这里被钉成三条可证伪的断言:

1. **不越界**:带 topic 时 matched_tables ⊆ topic.datasets(词法锚定、字段名
   锚定、KB term 表、指标扩展四条通道全试一遍 —— 尤其是指标扩展,它是唯一
   一处引擎自己往 matched 里加表的地方);
2. **真的更窄**:同一个问题,不带 topic 锚到域外数据集、带 topic 锚不到
   → 范围是被收敛过的,不是"恰好本来就窄";
3. **失败方向是显式拒绝**:域外问题 / 域不存在 → refusal(不静默全量回答)。
"""
from __future__ import annotations

from trove.services.kb.service import TermHit
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
    TopicDomain,
)
from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.state import WorkflowState


def _f(name: str, role: str = "") -> SemanticField:
    return SemanticField(name=name, expression=name, semantic_role=role)


def _model(topics: list[TopicDomain] | None = None) -> SemanticModel:
    """两域模型:loans{loan,account} / clients{client,district}。"""
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(
                name="loan", primary_key=["loan_id"],
                fields=[_f("loan_id", "identifier"), _f("amount", "measure")],
                synonyms=["loan"], description="loan records"),
            SemanticDataset(
                name="account", primary_key=["account_id"],
                fields=[_f("account_id", "identifier"), _f("district_id")],
                synonyms=["account"], description="bank accounts"),
            SemanticDataset(
                name="client", primary_key=["client_id"],
                fields=[_f("client_id", "identifier"), _f("gender", "enum")],
                synonyms=["client"], description="bank clients"),
            SemanticDataset(
                name="district", primary_key=["district_id"],
                fields=[_f("district_id", "identifier"), _f("region")],
                synonyms=["district"], description="districts"),
        ],
        relationships=[
            SemanticRelationship(
                name="loan_to_account", from_="loan", to="account",
                from_columns=["account_id"], to_columns=["account_id"],
                cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("total loan amount", "SUM(loan.amount)",
                           datasets=["loan"]),
            SemanticMetric("client count", "COUNT(client.client_id)",
                           datasets=["client"]),
        ],
        topics=topics or [
            TopicDomain(name="loans", description="贷款业务",
                        synonyms=["loan", "贷款"], datasets=["loan", "account"]),
            TopicDomain(name="clients", datasets=["client", "district"]),
        ],
    )


class FakeProvider:
    enabled = True

    def __init__(self, model: SemanticModel, terms: list[TermHit] | None = None):
        self._model = model
        self.terms = list(terms or [])

    def model(self):
        return self._model

    def terms_for(self, query, tables=None, all_tables=None):
        return list(self.terms)

    def field_hits(self, question, tables=None):
        return []


class FakeKB:
    """metric_family 的域外扩展源:命中「client count」并把 client 拉回候选。"""

    def __init__(self, terms: list[TermHit] | None = None):
        self.terms = list(terms or [])

    async def ensure_synced(self, default_datasource=None):
        return None

    async def search_terms(self, question, datasource, tables=None, all_tables=None):
        return list(self.terms)

    async def metric_family(self, question, datasource, matched_tables=None,
                            all_tables=None, **kw):
        metrics = [m for m in _model().metrics if m.name == "client count"]
        tables = list(matched_tables or [])
        for m in metrics:
            for d in m.datasets:
                if d and d not in tables:
                    tables.append(d)  # 域外扩展:client
        return {"metrics": metrics, "entities": [], "tables": tables}

    async def search_schema_docs(self, question, datasource, limit=5):
        return []

    def retrieval_backend_label(self, datasource):
        return "builtin"


def _state(question: str, topic: str = "") -> WorkflowState:
    return WorkflowState(
        session_id="s1", question=question, datasource="fin", topic=topic)


class TestTopicScopeConvergence:
    async def test_scope_filters_lexical_matching(self, sqlite_registry):
        """问题同时点名两个数据集 → 带 topic 只锚域内那个。"""
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("loan amount by client gender", topic="loans"))
        assert out["matched_tables"] == ["loan"]
        assert set(out["matched_tables"]) <= {"loan", "account"}

    async def test_scope_filters_term_tables(self, sqlite_registry):
        """KB term 命中域外表(client) → 被作用域挡下。"""
        kb = FakeKB(terms=[TermHit(
            term="client gender", aliases=["gender"], mapping="client.gender",
            tables=["client"], definition="")])
        node = make_schema_linking(
            kb=kb, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("client gender breakdown", topic="loans"))
        assert out.get("refusal"), out
        assert out["refusal"]["reason"] == "no_semantic_match"
        assert out["refusal"]["topic"] == "loans"

    async def test_scope_filters_metric_family_expansion(self, sqlite_registry):
        """指标扩展(引擎自己加表)也必须收敛 —— 这是最容易越界的一条。"""
        kb = FakeKB()
        node = make_schema_linking(
            kb=kb, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("loan amount by client gender", topic="loans"))
        assert out["matched_tables"] == ["loan"]  # client 未被指标扩展拉回
        assert "client count" in out["semantic_context"]  # 指标本身仍在召回里

    async def test_scope_actually_narrows(self, sqlite_registry):
        """同一个问题:不带 topic 锚到 client(域外),带 topic 收敛掉。"""
        provider = FakeProvider(_model())
        free = await make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=provider)(_state("client gender breakdown"))
        assert "client" in free["matched_tables"]  # 全模型下命中
        scoped = await make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=provider)(_state("client gender breakdown", topic="loans"))
        assert scoped.get("refusal")
        assert scoped["matched_tables"] == []

    async def test_topic_render_and_link_detail(self, sqlite_registry):
        """命中主题域:semantic_context 声明作用域;link_detail 记录事实。"""
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("loan amount", topic="loans"))
        assert out["semantic_context"].startswith("Topic domain: loans")
        assert "(scope: loan, account)" in out["semantic_context"]
        assert out["link_detail"]["topic"] == "loans"
        assert sorted(out["link_detail"]["topic_scope"]) == ["account", "loan"]

    async def test_no_topic_unchanged(self, sqlite_registry):
        """未选主题域:行为与不启用主题域完全一致(存量路径零变化)。"""
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("loan amount by client gender"))
        assert "topic" not in out["link_detail"]
        assert "Topic domain" not in out["semantic_context"]
        assert set(out["matched_tables"]) == {"loan", "client"}

    async def test_topic_name_is_trimmed_and_case_insensitive(self, sqlite_registry):
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("loan amount", topic="  LOANS "))
        assert out["link_detail"]["topic"] == "loans"

    async def test_unknown_topic_refuses(self, sqlite_registry):
        """名字解析不到 → 显式拒绝(不静默退回全量)。"""
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        out = await node(_state("loan amount", topic="ghost"))
        assert out["matched_tables"] == []
        assert out["refusal"]["reason"] == "topic_not_found"
        assert out["refusal"]["topic"] == "ghost"
        assert out["refusal"]["available_topics"] == ["loans", "clients"]
        assert out["semantic_context"] == ""

    async def test_stale_topic_scope_refuses(self, sqlite_registry):
        """域在、但声明的数据集全不在模型里 → topic_empty_scope(域过期)。"""
        model = _model(topics=[
            TopicDomain(name="legacy", datasets=["dropped_table"])])
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(model))
        out = await node(_state("loan amount", topic="legacy"))
        assert out["refusal"]["reason"] == "topic_empty_scope"
        assert out["link_detail"]["topic_status"] == "empty_scope"

    async def test_retry_round_still_scoped(self, sqlite_registry):
        """反思重跑轮放宽阈值/上限,但不得越过作用域。"""
        node = make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(_model()))
        state = _state("client gender by district", topic="loans")
        state.retry_count = 2
        out = await node(state)
        assert set(out["matched_tables"]) <= {"loan", "account"}
