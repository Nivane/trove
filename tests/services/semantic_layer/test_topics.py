"""业务主题域(topic domains)构造测试:解析 / lint / 合并。

主题域是语义模型之上的**分组与收敛边界**(Trove 扩展段,非 OSSIE):
``semantic_model[0].topics[]`` 声明 {name, datasets, metrics?, synonyms?}。
这三层测试对应它落地的三个前提:

1. 解析:可选段,缺省 = 存量模型零变化;坏形状丢弃而不是猜;
2. lint:边界不成立的形态全部拦下(悬空 dataset / 主题外 metric / 空边界);
3. 合并:多源合并与 kb init 三方合并都不吞掉主题域。
"""
from __future__ import annotations

import logging

from trove.services.kb.lint import lint_semantics, lint_semantics_document
from trove.services.kb.merge import merge3
from trove.services.semantic_layer.models import SemanticModel
from trove.services.semantic_layer.ossie import parse_ossie
from trove.services.semantic_layer.provider import _merge_models

_DOC = """
version: 0.2.0.dev0
semantic_model:
  - name: fin
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        fields:
          - name: loan_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan_id
          - name: amount
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: amount
      - name: account
        source: account
        primary_key: [account_id]
        fields:
          - name: account_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account_id
      - name: client
        source: client
        primary_key: [client_id]
        fields:
          - name: client_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: client_id
    metrics:
      - name: total_loan_amount
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
      - name: client_count
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(client.client_id)
    topics:
      - name: loans
        description: 贷款主题
        synonyms: [loan, 贷款]
        datasets: [loan, account]
        metrics: [total_loan_amount]
        examples: [每个地区的贷款金额是多少]
      - name: clients
        datasets: [client]
"""


def _model_dict(doc: str = _DOC) -> dict:
    """取 semantic_model[0] 的原始 dict(lint 判的是字形,不是解析产物)。"""
    import yaml

    return yaml.safe_load(doc)["semantic_model"][0]


# ── 解析 ──────────────────────────────────────────────────


class TestParseTopics:
    def test_topics_parsed(self):
        model = parse_ossie(_DOC, preferred_dialect="sqlite")
        assert [t.name for t in model.topics] == ["loans", "clients"]
        loans = model.topics[0]
        assert loans.datasets == ["loan", "account"]
        assert loans.metrics == ["total_loan_amount"]
        assert loans.synonyms == ["loan", "贷款"]
        assert loans.examples == ["每个地区的贷款金额是多少"]
        assert loans.description == "贷款主题"

    def test_absent_topics_is_empty(self):
        """存量模型(无 topics 键)一字不变:空列表,不是 None。"""
        doc = _DOC.replace("    topics:\n", "    x_topics:\n")
        model = parse_ossie(doc, preferred_dialect="sqlite")
        assert model.topics == []
        assert model.datasets  # 其余部分照常解析

    def test_bare_scalar_datasets_becomes_single(self):
        """裸标量(``datasets: loan``)按单元素读,与 _clean_values 同法。"""
        doc = _DOC.replace("        datasets: [loan, account]",
                           "        datasets: loan")
        model = parse_ossie(doc, preferred_dialect="sqlite")
        assert model.topics[0].datasets == ["loan"]

    def test_bad_shapes_dropped_not_guessed(self):
        """空名条目丢弃、映射型 datasets 忽略 —— 不猜。"""
        doc = _DOC.replace("      - name: clients\n        datasets: [client]",
                           "      - name: ''\n        datasets: [client]\n"
                           "      - name: broken\n        datasets: {a: b}")
        model = parse_ossie(doc, preferred_dialect="sqlite")
        assert [t.name for t in model.topics] == ["loans", "broken"]
        assert model.topics[1].datasets == []

    def test_top_level_topics_ignored_with_warning(self, caplog):
        """文档顶层的 topics 被忽略 —— 但必须出声(收敛边界不能静默消失)。"""
        doc = (
            "version: 1\n"
            "topics:\n  - name: loans\n    datasets: [loan]\n"
            + _DOC[_DOC.index("semantic_model:"):]
              .replace("    topics:\n", "    x_topics:\n")
        )
        with caplog.at_level(logging.WARNING):
            model = parse_ossie(doc, preferred_dialect="sqlite")
        assert model.topics == []  # 模型内那份被改名,不生效
        assert any("top level" in r.getMessage() for r in caplog.records)


# ── lint ─────────────────────────────────────────────────


class TestTopicLint:
    def test_valid_topics_no_issues(self):
        model = _model_dict()
        assert lint_semantics(model) == []
        assert lint_semantics_document(
            __import__("yaml").safe_load(_DOC)) == []

    def test_no_topics_key_is_clean(self):
        model = _model_dict().copy()
        model.pop("topics")
        assert lint_semantics(model) == []

    def test_empty_datasets_is_an_error(self):
        model = _model_dict()
        model["topics"][0]["datasets"] = []
        issues = lint_semantics(model)
        assert any("未声明任何数据集" in i and "loans" in i for i in issues)

    def test_missing_datasets_key_is_an_error(self):
        model = _model_dict()
        model["topics"][0].pop("datasets")
        assert any("未声明任何数据集" in i for i in lint_semantics(model))

    def test_undeclared_dataset_is_an_error(self):
        model = _model_dict()
        model["topics"][0]["datasets"] = ["loan", "ghost"]
        issues = lint_semantics(model)
        assert any("引用未声明的数据集 ghost" in i for i in issues)

    def test_undeclared_metric_is_an_error(self):
        model = _model_dict()
        model["topics"][0]["metrics"] = ["no_such_metric"]
        issues = lint_semantics(model)
        assert any("引用未声明的指标 no_such_metric" in i for i in issues)

    def test_metric_anchored_outside_topic_is_an_error(self):
        """client_count 锚定 client,而 loans 主题只含 loan/account → 自相矛盾。"""
        model = _model_dict()
        model["topics"][0]["metrics"] = ["total_loan_amount", "client_count"]
        issues = lint_semantics(model)
        assert any("锚定到主题外的数据集 client" in i for i in issues)

    def test_duplicate_and_empty_names_are_errors(self):
        model = _model_dict()
        model["topics"].append({"name": "loans", "datasets": ["loan"]})
        model["topics"].append({"name": "", "datasets": ["loan"]})
        issues = lint_semantics(model)
        assert any("重复定义" in i for i in issues)
        assert any("缺少 name" in i for i in issues)

    def test_empty_synonym_is_an_error(self):
        model = _model_dict()
        model["topics"][0]["synonyms"] = ["loan", "  "]
        assert any("synonyms" in i for i in lint_semantics(model))

    def test_non_list_topics_is_an_error(self):
        model = _model_dict()
        model["topics"] = {"name": "loans"}
        assert any("topics 必须是数组" in i for i in lint_semantics(model))

    def test_top_level_topics_document_issue(self):
        """文档顶层写 topics:写盘门禁拦下(与 masking 同款错层拦截)。"""
        import yaml

        doc = yaml.safe_load(_DOC)
        doc["topics"] = doc["semantic_model"][0].pop("topics")
        issues = lint_semantics_document(doc)
        assert any("文档顶层的 topics 会被忽略" in i for i in issues)


# ── 合并 ─────────────────────────────────────────────────


class TestTopicMerge:
    def test_provider_merge_carries_topics_by_name(self):
        """多源合并:同名主题域以 override(KB)为准,不同名并列。"""
        base = SemanticModel(name="base", topics=[
            parse_ossie(_DOC).topics[0],  # loans
        ])
        override = SemanticModel(name="override", topics=[
            parse_ossie(_DOC).topics[1],  # clients
        ])
        merged = _merge_models(base, override)
        assert sorted(t.name for t in merged.topics) == ["clients", "loans"]

    def test_provider_merge_same_name_override_wins(self):
        base = SemanticModel(name="base", topics=[
            parse_ossie(_DOC).topics[0],
        ])
        other = _DOC.replace("        datasets: [loan, account]",
                             "        datasets: [loan]")
        override = SemanticModel(name="override", topics=[
            parse_ossie(other).topics[0],
        ])
        merged = _merge_models(base, override)
        assert [t.datasets for t in merged.topics] == [["loan"]]

    def test_kb_init_three_way_merge_keeps_hand_written_topics(self):
        """kb init --overwrite 走三方合并:人写的 topics 必须存活。

        生成方(``theirs``)不认识 topics 这个键 → 判定为"人手工加的"
        (base 无 / ours 有 / theirs 无 → ours),不销毁。
        """
        import yaml

        doc = yaml.safe_load(_DOC)
        base = {"semantic_model": [{"name": "fin", "datasets": [], "metrics": []}]}
        ours = yaml.safe_load(_DOC)
        theirs = {"semantic_model": [{"name": "fin", "datasets": [
            {"name": "loan", "fields": []}], "metrics": []}]}
        result = merge3(base, ours, theirs, "semantics.yml")
        topics = result.doc["semantic_model"][0].get("topics") or []
        assert [t["name"] for t in topics] == ["loans", "clients"]
        assert doc["semantic_model"][0]["topics"]  # 源文档未被改动
