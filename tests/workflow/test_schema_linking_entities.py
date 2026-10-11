"""E4 · schema_linking 消费 O1:别名锚定 + 对象摘要行(本体形态批次)。

两条断言的共同纪律是**兼容安全带**(设计 §4):未声明 entity / 未声明
别名时,打分与渲染与现状逐字节一致 —— 声明是唯一的分叉点。

1. **单通道合并**(design D2):``entity.aliases`` 并入既有的 synonyms
   子串 3.0 子句,不新开通道 —— 于是 in_scope 收敛、declared 过滤、
   阈值/上限这些既有逻辑一个都不用复制,也不可能漂移。其余打分子句
   (2.5 词重叠 / 2.0 / 1.5 描述 / 1.0 弱信号)一行未动,单独钉。
2. **对象摘要行**(设计 §5.1):``_render_semantic_context`` 只在 entity
   已声明时多渲染一行 ``Object: ...``;dataset.synonyms 的渲染现状不动
   (N20:它从来只渲染字段 synonyms,不渲染 dataset.synonyms)。
"""
from __future__ import annotations

from trove.services.kb.service import KbService
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)
from trove.workflow.nodes.schema_linking import (
    _object_summary_line,
    _render_semantic_context,
    _semantic_dataset_score,
    _semantic_linking,
    _semantic_match_datasets,
    _word_tokens,
)

SEMANTICS_PLAIN = """
version: "0.1"
semantic_model:
  - name: demo
    datasets:
      - name: loan
        fields:
          - name: loan_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.loan_id
            datatype: Integer
            semantic_role: identifier
          - name: amount
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.amount
            datatype: Integer
            semantic_role: measure
        description: loan records
    metrics:
      - name: 平均贷款金额
        description: 所有贷款的平均金额
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: AVG(loan.amount)
"""

SEMANTICS_ENTITY = """
version: "0.1"
semantic_model:
  - name: demo
    datasets:
      - name: loan
        entity:
          display_name: 贷款
          natural_keys:
            - loan_id
          aliases:
            - 借据单
            - loan note
        fields:
          - name: loan_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.loan_id
            datatype: Integer
            semantic_role: identifier
          - name: amount
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.amount
            datatype: Integer
            semantic_role: measure
        description: loan records
    metrics:
      - name: 平均贷款金额
        description: 所有贷款的平均金额
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: AVG(loan.amount)
"""


def _f(name: str, role: str = "dimension") -> SemanticField:
    return SemanticField(name=name, expression=name, semantic_role=role)


def _entity_model(entity: SemanticEntity | None = None) -> SemanticModel:
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(
                name="loan", primary_key=["loan_id"],
                fields=[_f("loan_id", "identifier"), _f("amount", "measure")],
                synonyms=[], description="loan records", entity=entity,
            ),
            SemanticDataset(
                name="district", primary_key=["district_id"],
                fields=[_f("district_id", "identifier"), _f("region")],
                synonyms=[], description="district records",
            ),
        ],
        metrics=[
            SemanticMetric("total loan amount", "SUM(loan.amount)",
                           datasets=["loan"]),
        ],
    )


class _Provider:
    """最小 SemanticLayer stub(与既有 schema_linking 测试同款)。"""

    enabled = True

    def __init__(self, model: SemanticModel):
        self._model = model

    def model(self):
        return self._model

    def terms_for(self, query):
        return []

    def field_hits(self, question, matched):
        return []


# ── ① 别名并入 synonyms 3.0 子句(单通道合并) ─────────────────────


def test_alias_substring_hit_scores_the_synonym_clause():
    """别名子串命中 → 3.0,与 synonym 同分(同一子句,不是新通道)。"""
    entity = SemanticEntity(display_name="贷款", aliases=["借据单"])
    d = _entity_model(entity).datasets[0]
    assert _semantic_dataset_score(d, "借据单的金额是多少", _word_tokens("借据单的金额是多少")) == 3.0
    # synonym 命中仍是同一个 3.0(单通道:两者不可区分)
    d_syn = SemanticDataset(
        name="loan", primary_key=["loan_id"], fields=[_f("loan_id")],
        synonyms=["借据单"], description="loan records",
    )
    assert _semantic_dataset_score(
        d_syn, "借据单的金额是多少", _word_tokens("借据单的金额是多少")) == 3.0


def test_alias_anchors_dataset_chinese():
    """中文别名命中 → 数据集进入 matched(阈值 2.0 之上)。"""
    model = _entity_model(SemanticEntity(display_name="贷款", aliases=["借据单"]))
    matched = _semantic_match_datasets(model, "借据单的金额是多少", [], None)
    assert "loan" in matched


def test_alias_anchors_dataset_english():
    """英文别名(多词)整串子串命中 → 锚定。"""
    model = _entity_model(SemanticEntity(aliases=["loan note"]))
    matched = _semantic_match_datasets(
        model, "how many loan note were issued", [], None)
    assert "loan" in matched


def test_alias_hit_is_case_insensitive():
    model = _entity_model(SemanticEntity(aliases=["LoanNote"]))
    assert "loan" in _semantic_match_datasets(
        model, "count of LOANNOTE rows", [], None)


def test_alias_anchor_respects_topic_scope():
    """别名命中不做 in_scope 的例外 —— 单通道合并的直接好处。

    新开通道就要复制 in_scope 过滤;合并子句天然继承既有的收敛逻辑。
    """
    model = _entity_model(SemanticEntity(aliases=["借据单"]))
    matched = _semantic_match_datasets(
        model, "借据单的金额是多少", [], None, scope={"district"})
    assert matched == []


def test_undeclared_alias_scoring_is_byte_identical():
    """未声明 entity / 空 aliases → 打分与现状逐字节一致(兼容安全带)。"""
    q = "average amount per region"
    bare = SemanticDataset(
        name="loan", primary_key=["loan_id"], fields=[_f("loan_id")],
        synonyms=["loans"], description="loan records",
    )
    declared_empty = SemanticDataset(
        name="loan", primary_key=["loan_id"], fields=[_f("loan_id")],
        synonyms=["loans"], description="loan records",
        entity=SemanticEntity(display_name="贷款"),  # aliases 为空
    )
    for question in (q, "loans by district", "unrelated words here"):
        assert _semantic_dataset_score(
            bare, question, _word_tokens(question)) == _semantic_dataset_score(
            declared_empty, question, _word_tokens(question))


def test_alias_does_not_feed_the_other_scoring_clauses():
    """别名不进 2.5 词重叠 / 1.0 弱信号子句(其余打分逻辑一行不动)。

    别名词元在问句里出现、但整串不构成子串时,分数与**去掉别名**的同一
    数据集相同 —— 别名只能在 3.0 子句里说话。
    """
    q = "gamma delta report"
    entity = SemanticEntity(aliases=["gamma delta sigma"])
    with_alias = _entity_model(entity).datasets[0]
    without_alias = _entity_model(None).datasets[0]
    assert _semantic_dataset_score(
        with_alias, q, _word_tokens(q)) == _semantic_dataset_score(
        without_alias, q, _word_tokens(q))


# ── ② 对象摘要行(entity 已声明才渲染)────────────────────────────


def _declared(**kw) -> SemanticDataset:
    base = dict(
        name="loan", primary_key=["loan_id"],
        fields=[_f("loan_id", "identifier"), _f("amount", "measure")],
        synonyms=["loans"], description="loan records",
    )
    base.update(kw)
    return SemanticDataset(**base)


def test_object_summary_line_full_shape():
    d = _declared(entity=SemanticEntity(
        display_name="贷款", aliases=["贷款", "借据单"], natural_keys=["loan_id"]))
    assert _object_summary_line(d) == (
        "Object: 贷款 (aliases: 贷款, 借据单; natural keys: loan_id)")


def test_object_summary_line_display_name_falls_back_to_dataset_name():
    d = _declared(entity=SemanticEntity(aliases=["借据单"]))
    assert _object_summary_line(d) == "Object: loan (aliases: 借据单; natural keys: loan_id)"


def test_object_summary_line_natural_keys_fall_back_to_primary_key():
    d = _declared(entity=SemanticEntity(display_name="贷款", aliases=["借据单"]))
    assert _object_summary_line(d) == (
        "Object: 贷款 (aliases: 借据单; natural keys: loan_id)")


def test_object_summary_line_omits_empty_segments():
    """别名空 → 该段不渲染;自然键也空(无 primary_key)→ 只剩显示名。"""
    no_aliases = _declared(entity=SemanticEntity(display_name="贷款"))
    assert _object_summary_line(no_aliases) == "Object: 贷款 (natural keys: loan_id)"
    bare = _declared(primary_key=[], entity=SemanticEntity(display_name="贷款"))
    assert _object_summary_line(bare) == "Object: 贷款"


def test_object_summary_line_absent_when_entity_undeclared():
    assert _object_summary_line(_declared()) == ""


def test_render_context_declared_has_object_line_after_dataset_line():
    model = _entity_model(SemanticEntity(
        display_name="贷款", aliases=["贷款", "借据单"], natural_keys=["loan_id"]))
    ctx = _render_semantic_context(model, ["loan"], None, "loan amount")
    assert "Dataset: loan\nObject: 贷款 (aliases: 贷款, 借据单; natural keys: loan_id)" in ctx


def test_render_context_undeclared_is_byte_identical():
    """未声明 → 渲染文本逐字节不变(既有 prompt 快照口径:整段字符串比较)。"""
    ctx = _render_semantic_context(_entity_model(None), ["loan"], None, "loan amount")
    assert ctx == (
        "Dataset: loan\n"
        "Description: loan records\n"
        "Fields:\n"
        "  - loan_id | role=identifier\n"
        "  - amount | role=measure\n"
        "Metrics:\n"
        "  - total loan amount = SUM(loan.amount)"
    )


def test_render_context_no_object_line_for_duck_typed_datasets():
    """鸭子类型测试替身(无 entity 属性)照旧 —— getattr 缺席容忍。"""

    class _D:
        name = "loan"
        description = "loan records"
        synonyms = ["loans"]
        fields: list = []

    class _M:
        datasets = [_D()]
        metrics: list = []
        instructions = ""

        def __init__(self):
            self.fan_out = None

    ctx = _render_semantic_context(_M(), ["loan"], None, "loan")
    assert ctx.startswith("Dataset: loan\n")
    assert "Object:" not in ctx


def test_render_context_never_renders_dataset_synonyms():
    """N20 现状不动:渲染的是字段 synonyms,dataset.synonyms 从不出现。

    声明 entity(带别名)也不改变这一点 —— 别名的可见面是对象摘要行,
    不是把 dataset.synonyms 悄悄带出来。
    """
    model = _entity_model(SemanticEntity(display_name="贷款", aliases=["借据单"]))
    model.datasets[0].synonyms = ["loans"]
    ctx = _render_semantic_context(model, ["loan"], None, "loan")
    assert "synonyms: loans" not in ctx


# ── ③ 全链:_semantic_linking 直通(语义模型 → semantic_context)──


def _state(question, datasource="demo"):
    class _S:
        error_analysis = ""
        error = None

    s = _S()
    s.question = question
    s.datasource = datasource
    return s


async def _kb(tmp_path, text=SEMANTICS_ENTITY):
    d = tmp_path / "kb" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "semantics.yml").write_text(text, encoding="utf-8")
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
    await kb.ensure_synced("demo")
    return kb


async def test_full_path_alias_anchors_and_renders_object_line(tmp_path):
    """全链:YAML 声明 entity → 别名锚定 loan → 上下文带对象摘要行。"""
    kb = await _kb(tmp_path)
    q = "借据单的平均金额"
    state = _state(q)
    term_hits = await kb.search_terms(q, "demo")
    provider = await _live_provider(tmp_path)
    base = await _semantic_linking(
        state, kb, None, provider, term_hits, q, "demo")
    assert base.get("refusal") is None
    assert "loan" in base["matched_tables"]
    assert "Object: 贷款 (aliases: 借据单, loan note; natural keys: loan_id)" in base["semantic_context"]


async def test_full_path_undeclared_yaml_render_is_byte_identical(tmp_path):
    """YAML 无 entity 子块 → 渲染与今天逐字节一致(兼容安全带的全链面)。"""
    kb = await _kb(tmp_path, SEMANTICS_PLAIN)
    provider = await _live_provider(tmp_path)
    q = "loan amount"
    base = await _semantic_linking(
        _state(q), kb, None, provider,
        await kb.search_terms(q, "demo"), q, "demo")
    ctx = base["semantic_context"]
    assert "Object:" not in ctx
    assert "Dataset: loan\nDescription: loan records" in ctx


async def _live_provider(tmp_path):
    """从 tmp KB 的 semantics.yml 起一个真 provider(kb.init 的最小替代)。"""
    from trove.services.semantic_layer.provider import SemanticLayerProvider

    ds_dir = tmp_path / "kb" / "demo"
    return SemanticLayerProvider(
        ds_dir, "demo", kb_semantics_path=ds_dir / "semantics.yml")
