"""Intent classification tests — query vs metadata (two-way) design."""

from trove.workflow.intent import (
    Intent,
    chitchat_subtype,
    classify_intent,
    has_followup_signal,
    has_strong_chitchat,
    has_strong_correction,
    has_strong_write,
    has_weak_signal,
    last_user_question,
    verify_intent,
)


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
            "平均贷款金额是什么口径",
            "客户数的定义是什么",
            "知识库里有哪些内容",
            "有哪些参考 SQL",
            "account 的血缘",
            "loan 表的数据来源",
        ]:
            assert classify_intent(q) == Intent.METADATA, q

    def test_metadata_weak_signals_trigger_llm_confirm(self):
        """变体/模糊问法 → 弱信号（LLM 二分类确认），不再强路由。"""
        for q in [
            "loan 表有哪些字段",
            "loan 和 order 表分别什么含义？有什么关系",
            "表之间的关系",
            "district 与 account 通过什么字段关连",
            "account 与 loan 怎么连接",
        ]:
            assert classify_intent(q) is None, q
            assert has_weak_signal(q), q


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

    def test_parse_new_intents(self):
        from trove.workflow.intent import parse_llm_intent

        assert parse_llm_intent("write") == Intent.WRITE
        assert parse_llm_intent("chitchat.") == Intent.CHITCHAT
        assert parse_llm_intent("CORRECTION") == Intent.CORRECTION


class TestWriteSignals:
    def test_strong_write_phrases(self):
        for q in [
            "删除loan表的记录",
            "插入一条新记录",
            "把重复记录删掉",
            "清空account表",
            "增删改查",
            "update the loan table",
            "drop table loan",
            "insert into loan values (1)",
            "delete from loan",
        ]:
            assert classify_intent(q) == Intent.WRITE, q

    def test_freshness_and_filter_context_are_not_write(self):
        """数据新鲜度问法与「被删除」过滤语境不算写意图。"""
        for q in [
            "数据更新到几号",
            "被删除的记录有多少",
            "创建时间字段的含义是什么",
        ]:
            assert classify_intent(q) is None, q

    def test_write_wins_over_chitchat_and_metadata(self):
        assert classify_intent("你好,把重复记录删掉") == Intent.WRITE
        assert classify_intent("如何修改loan表的表结构") == Intent.WRITE


class TestChitchatSignals:
    def test_pure_chitchat_is_strong(self):
        for q in [
            "你好", "你好!", "您好", "hi", "hello",
            "谢谢", "感谢", "多谢", "再见", "拜拜",
            "你是谁", "你能做什么", "怎么用", "你呢",
        ]:
            assert classify_intent(q) == Intent.CHITCHAT, q

    def test_greeting_with_data_question_escapes(self):
        """问候前缀 + 数据问题 → 不是纯闲聊(交给 LLM/query)。"""
        for q in [
            "你好,哪个地区平均贷款最高",
            "谢谢,再帮我查下loan表",
            "hi,贷款总额是多少",
        ]:
            assert classify_intent(q) is None, q


class TestCorrectionSignals:
    def test_pure_feedback_is_correction(self):
        for q in [
            "不对", "错了", "不对吧", "重算", "再算一次",
            "重新算", "结果不对", "数字错了", "recalculate", "wrong",
        ]:
            assert classify_intent(q) == Intent.CORRECTION, q

    def test_feedback_with_substance_escapes(self):
        """带新数据的纠正 → 是新的 query,不是纯反馈。"""
        for q in [
            "不对,用日均余额口径重算",
            "不对,北京应该是100万",
            "不对,把时间改成最近7天",
        ]:
            assert classify_intent(q) is None, q


class TestFollowupSignal:
    def test_elliptical_followups(self):
        history = "user: 哪个地区平均贷款最高?\nassistant: 北京\n"
        for q in ["那北京呢", "北京呢", "最高的呢", "那杭州和苏州呢", "那其他地区呢"]:
            assert has_followup_signal(q, history) is True, q

    def test_no_history_or_self_contained(self):
        history = "user: 哪个地区平均贷款最高?\nassistant: 北京\n"
        assert has_followup_signal("那北京呢", "") is False
        assert has_followup_signal("哪个地区的平均贷款金额最高", history) is False
        assert has_followup_signal("", history) is False


class TestChitchatSubtype:
    def test_subtypes(self):
        assert chitchat_subtype("你好") == "greet"
        assert chitchat_subtype("谢谢") == "thanks"
        assert chitchat_subtype("再见") == "bye"
        assert chitchat_subtype("你是谁") == "capability"
        assert chitchat_subtype("嘿嘿") == "other"


class TestLastUserQuestion:
    def test_parses_last_user_line(self):
        assert last_user_question(
            "user: 北京的平均贷款\nassistant: 100\nuser: 那上海呢\nassistant: 200"
        ) == "那上海呢"
        assert last_user_question("user: 第一个问题\n") == "第一个问题"
        assert last_user_question("") is None
        assert last_user_question("[summary] 概要\nuser: 旧\nassistant: 答") == "旧"


class TestVerifyIntent:
    """LLM 裁决的确定性验证。"""

    def test_metadata_with_evidence_passes(self):
        """metadata 裁决有任一实质证据（强信号/已知表/术语命中）→ 通过。"""
        assert verify_intent(Intent.METADATA, strong_match=True, mentioned_table=False, term_hit=False, data_signal=False) == Intent.METADATA
        assert verify_intent(Intent.METADATA, strong_match=False, mentioned_table=True, term_hit=False, data_signal=False) == Intent.METADATA
        assert verify_intent(Intent.METADATA, strong_match=False, mentioned_table=False, term_hit=True, data_signal=False) == Intent.METADATA

    def test_metadata_without_evidence_falls_back_to_query(self):
        """metadata 裁决无实质 → 宽容回退 query（防幻觉方向）。"""
        assert verify_intent(Intent.METADATA, strong_match=False, mentioned_table=False, term_hit=False, data_signal=False) == Intent.QUERY

    def test_query_overridden_by_strong_signal(self):
        """LLM 说 query 但强 metadata 信号且无数据问题信号 → 覆写 metadata。"""
        assert verify_intent(Intent.QUERY, strong_match=True, mentioned_table=False, term_hit=False, data_signal=False) == Intent.METADATA

    def test_query_with_data_signal_or_no_strong_stays(self):
        assert verify_intent(Intent.QUERY, strong_match=True, mentioned_table=False, term_hit=False, data_signal=True) == Intent.QUERY
        assert verify_intent(Intent.QUERY, strong_match=False, mentioned_table=False, term_hit=False, data_signal=False) == Intent.QUERY

    def test_write_signal_wins_over_everything(self):
        """写意图是安全信号:无论 LLM 与证据如何,正则命中即拒绝。"""
        assert verify_intent(Intent.QUERY, write_signal=True, strong_match=True, data_signal=True) == Intent.WRITE
        assert verify_intent(Intent.METADATA, write_signal=True, mentioned_table=True) == Intent.WRITE

    def test_llm_write_verdict_trusted(self):
        """LLM 判 write 但正则未命中:信任(拒绝是安全失败方向)。"""
        assert verify_intent(Intent.WRITE, write_signal=False) == Intent.WRITE

    def test_metadata_strong_overrides_llm_write(self):
        """强 metadata 信号且无数据信号 → metadata 优先于 LLM 的 write 误判。"""
        assert verify_intent(Intent.WRITE, strong_match=True, data_signal=False) == Intent.METADATA

    def test_followup_requires_history(self):
        """省略式追问必须有历史才算 correction。"""
        assert verify_intent(Intent.QUERY, followup_signal=True, history_present=True) == Intent.CORRECTION
        assert verify_intent(Intent.QUERY, followup_signal=True, history_present=False) == Intent.QUERY

    def test_pure_correction_signal_requires_no_substance(self):
        """纯反馈词 + 无数据/无元数据实质 → correction;有实质 → 按 query 走。"""
        assert verify_intent(Intent.QUERY, correction_signal=True, data_signal=False, weak_signal=False) == Intent.CORRECTION
        assert verify_intent(Intent.QUERY, correction_signal=True, data_signal=True, weak_signal=False) == Intent.QUERY
        assert verify_intent(Intent.QUERY, correction_signal=True, data_signal=False, weak_signal=True) == Intent.QUERY

    def test_chitchat_needs_signal_and_no_data_question(self):
        assert verify_intent(Intent.CHITCHAT, chitchat_signal=True, data_signal=False) == Intent.CHITCHAT
        assert verify_intent(Intent.CHITCHAT, chitchat_signal=True, data_signal=True) == Intent.QUERY
        assert verify_intent(Intent.CHITCHAT, chitchat_signal=False, data_signal=False) == Intent.QUERY

    def test_chitchat_signal_routes_even_when_llm_says_query(self):
        """LLM 不可用/误判时,纯闲聊信号 + 无数据信号 → 直接路由。"""
        assert verify_intent(Intent.QUERY, chitchat_signal=True, data_signal=False) == Intent.CHITCHAT

    def test_llm_correction_trusted_with_history(self):
        """LLM 判 correction(正则未命中)→ 有历史则信任(重跑上一问)。"""
        assert verify_intent(Intent.CORRECTION, history_present=True) == Intent.CORRECTION
        assert verify_intent(Intent.CORRECTION, history_present=False) == Intent.QUERY
