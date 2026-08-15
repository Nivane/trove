"""Intent routing tests — classify user intent before the SQL pipeline."""

from trove.workflow.intent import Intent, classify_intent


class TestClassifyIntent:
    def test_query_default(self):
        assert classify_intent("哪个地区的平均贷款金额最高?") == Intent.QUERY
        assert classify_intent("how many accounts have loans") == Intent.QUERY

    def test_schema_intent(self):
        assert classify_intent("有哪些表") == Intent.SCHEMA
        assert classify_intent("loan 表有哪些字段") == Intent.SCHEMA
        assert classify_intent("表结构") == Intent.SCHEMA

    def test_semantic_intent(self):
        assert classify_intent("平均贷款金额是什么口径") == Intent.SEMANTIC
        assert classify_intent("客户数的定义是什么") == Intent.SEMANTIC

    def test_knowledge_intent(self):
        assert classify_intent("知识库里有哪些内容") == Intent.KNOWLEDGE
        assert classify_intent("有哪些参考 SQL") == Intent.KNOWLEDGE
        assert classify_intent("学过哪些模板") == Intent.KNOWLEDGE

    def test_lineage_intent(self):
        assert classify_intent("district 和哪些表关联") == Intent.LINEAGE
        assert classify_intent("loan 表的数据来源") == Intent.LINEAGE
        assert classify_intent("account 的血缘") == Intent.LINEAGE

    def test_priority_lineage_over_schema(self):
        """「表之间的关联」是血缘而非 schema。"""
        assert classify_intent("这些表之间的关联关系") == Intent.LINEAGE
