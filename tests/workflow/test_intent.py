"""Intent classification tests — query vs metadata (two-way) design."""

from trove.workflow.intent import Intent, classify_intent, has_weak_signal


class TestStrongSignals:
    def test_query_default_is_no_strong_signal(self):
        """普通数据问题无强信号 → None（路由层兜底为 query）。"""
        assert classify_intent("哪个地区的平均贷款金额最高?") is None
        assert classify_intent("how many accounts have loans") is None
        assert classify_intent("哪些客户的贷款金额超过 10 万") is None

    def test_metadata_strong_signals(self):
        for q in [
            "有哪些表",
            "list tables",
            "表结构",
            "loan 表有哪些字段",
            "平均贷款金额是什么口径",
            "客户数的定义是什么",
            "知识库里有哪些内容",
            "有哪些参考 SQL",
            "account 的血缘",
            "loan 表的数据来源",
            "loan 和 order 表分别什么含义？有什么关系",
            "表之间的关系",
        ]:
            assert classify_intent(q) == Intent.METADATA, q


class TestWeakSignals:
    def test_bare_table_mention_is_weak(self):
        """裸「表」字（可能是数据问题）→ 弱信号交给 LLM 二分类。"""
        assert classify_intent("loan 表的贷款总额是多少") is None
        assert has_weak_signal("loan 表的贷款总额是多少")

    def test_no_signal_query(self):
        assert not has_weak_signal("哪个地区的平均贷款金额最高?")


class TestParseLLMIntent:
    def test_parse_variants(self):
        from trove.workflow.intent import parse_llm_intent

        assert parse_llm_intent("query") == Intent.QUERY
        assert parse_llm_intent("Metadata.") == Intent.METADATA
        assert parse_llm_intent("  METADATA\n") == Intent.METADATA
        assert parse_llm_intent("The intent is schema") is None
        assert parse_llm_intent("") is None
