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


def _state(question: str, topic: str = "",
           principal: dict | None = None) -> WorkflowState:
    return WorkflowState(
        session_id="s1", question=question, datasource="fin", topic=topic,
        principal=principal)


def _wire(subject: str = "7", *, role: str = "user",
          topic_grants: dict | None = None) -> dict:
    """一张 wire 形状的主体(与 ``principal_to_wire`` 同形,手写以便直读)。"""
    from trove.services.authz.policy import Principal, principal_to_wire

    return principal_to_wire(Principal(
        subject=subject, role=role, grants=frozenset({"fin"}),
        topic_grants=None if topic_grants is None else {
            ds: frozenset(ts) for ds, ts in topic_grants.items()}))


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


class TestTopicGrantsEnforcement:
    """域级授权(topic_grants)在提问路径上的收窄。

    核心不变量:**不可见的域与不存在的域输出逐字节同形** —— 存在性不得从
    拒绝里漏出去(否则一次试探就拿到了域名单)。域级收窄收的是"域的入口面"
    (选择器 + 显式带域提问);数据访问边界仍是数据源门,未选域的提问不因
    本层而改变(见 test_no_topic_ask_is_not_gated)。
    """

    def _node(self, sqlite_registry, model=None):
        return make_schema_linking(
            kb=None, connectors=sqlite_registry,
            semantic_layer=FakeProvider(model or _model()))

    async def test_unauthorized_topic_is_byte_identical_to_not_found(
        self, sqlite_registry,
    ):
        """**存在性预言机的正面封口**:同一个输入「clients」,一次是"存在但无权"
        (模型里真有 clients),一次是"压根不存在"(模型里没有这个域)——
        两次的全部输出必须逐字节相同,包括被回显的输入本身。"""
        with_topic = self._node(sqlite_registry)          # 模型含 clients
        without_topic = self._node(sqlite_registry, _model(topics=[
            TopicDomain(name="loans", datasets=["loan", "account"])]))
        wire = _wire(topic_grants={"fin": ["loans"]})
        denied = await with_topic(
            _state("loan amount", topic="clients", principal=wire))
        missing = await without_topic(
            _state("loan amount", topic="clients", principal=wire))
        assert denied == missing
        assert denied["refusal"]["reason"] == "topic_not_found"
        assert denied["matched_tables"] == []
        assert denied["link_detail"]["topic_status"] == "not_found"

    async def test_unauthorized_output_differs_from_not_found_only_by_the_echo(
        self, sqlite_registry,
    ):
        """换个名字试探也拿不到信息:剔除对**用户自己的输入**的回显后,
        无权拒绝与不存在拒绝的其余字段完全一致(可用域清单也是同一份)。"""
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={"fin": ["loans"]})
        denied = await node(_state("loan amount", topic="clients", principal=wire))
        missing = await node(_state("loan amount", topic="ghost", principal=wire))
        drop = lambda d: {k: v for k, v in d.items() if k != "topic"}  # noqa: E731
        assert drop(denied["refusal"]) == drop(missing["refusal"])
        assert drop(denied["link_detail"]) == drop(missing["link_detail"])

    async def test_stale_invisible_topic_is_also_not_found(self, sqlite_registry):
        """域过期但**不可见**:报 not_found 而不是 empty_scope(后者也是存在性)。"""
        model = _model(topics=[
            TopicDomain(name="legacy", datasets=["dropped_table"]),
            TopicDomain(name="loans", datasets=["loan", "account"]),
        ])
        node = self._node(sqlite_registry, model)
        wire = _wire(topic_grants={"fin": ["loans"]})
        denied = await node(_state("loan amount", topic="legacy", principal=wire))
        assert denied["refusal"]["reason"] == "topic_not_found"
        assert denied["link_detail"]["topic_status"] == "not_found"

    async def test_authorized_topic_flows_normally(self, sqlite_registry):
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={"fin": ["loans"]})
        out = await node(_state("loan amount", topic="loans", principal=wire))
        assert out["matched_tables"] == ["loan"]
        assert out["link_detail"]["topic"] == "loans"

    async def test_authorization_compares_canonical_name(self, sqlite_registry):
        """授权清单写的是模型里的规范名;请求侧的大小写/空白由解析层收敛。"""
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={"fin": ["loans"]})
        out = await node(_state("loan amount", topic="  LOANS ", principal=wire))
        assert out["link_detail"]["topic"] == "loans"

    async def test_guidance_lists_only_visible_topics(self, sqlite_registry):
        """拒绝文案的 available_topics 按主体过滤 —— 预言机的封口在这里。"""
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={"fin": ["loans"]})
        denied = await node(_state("loan amount", topic="ghost", principal=wire))
        assert denied["refusal"]["available_topics"] == ["loans"]

    async def test_out_of_scope_guidance_is_filtered_too(self, sqlite_registry):
        """域内零锚定的"换个域"建议同样只列可见域(第二条泄露路径)。"""
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={"fin": ["loans"]})
        out = await node(_state("client gender", topic="loans", principal=wire))
        assert out["refusal"]["reason"] == "no_semantic_match"
        assert out["refusal"]["topic"] == "loans"
        assert out["refusal"]["available_topics"] == ["loans"]

    async def test_empty_map_hides_every_topic(self, sqlite_registry):
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={})
        out = await node(_state("loan amount", topic="loans", principal=wire))
        assert out["refusal"]["reason"] == "topic_not_found"
        assert out["refusal"]["available_topics"] == []

    async def test_admin_principal_sees_all(self, sqlite_registry):
        node = self._node(sqlite_registry)
        wire = _wire(role="admin", topic_grants={"fin": []})
        out = await node(_state("loan amount", topic="loans", principal=wire))
        assert out["matched_tables"] == ["loan"]

    async def test_unrestricted_principal_unchanged(self, sqlite_registry):
        """主题收窄未配置(存量用户)→ 与无主体时完全一致。"""
        node = self._node(sqlite_registry)
        anon = await node(_state("loan amount", topic="loans"))
        with_wire = await node(_state(
            "loan amount", topic="loans", principal=_wire()))
        assert with_wire == anon

    async def test_no_topic_ask_is_not_gated(self, sqlite_registry):
        """**边界自证**:未选主题域的提问不因域级授权改变 —— 本层收窄的是
        "域的选择面",数据访问边界仍是数据源门(在 API 层先行)。"""
        node = self._node(sqlite_registry)
        wire = _wire(topic_grants={})
        out = await node(_state("loan amount by client gender", principal=wire))
        assert set(out["matched_tables"]) == {"loan", "client"}
