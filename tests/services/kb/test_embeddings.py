"""Pure-stdlib hashed n-gram embedding tests — determinism, bilingual features,
hybrid rerank gating (det=0 stays excluded), near-duplicate detection.
"""

from __future__ import annotations

from trove.services.kb.embeddings import (
    cosine,
    coverage_score,
    embed,
    near_duplicate,
    rerank_score,
    similarity,
    text_features,
)


class TestTextFeatures:
    def test_english_words(self):
        feats = text_features("how many loans per year")
        assert "loans" in feats
        assert "year" in feats
        # 停用词/单字符被剔除
        assert "how" not in feats
        assert "a" not in feats

    def test_cjk_char_and_ngrams(self):
        feats = text_features("每年贷款次数")
        assert "贷" in feats
        assert "贷款" in feats
        assert "贷款次" in feats

    def test_empty(self):
        assert text_features("") == set()


class TestSimilarity:
    def test_self_is_high(self):
        assert similarity("每年贷款次数", "每年贷款次数") >= 0.999

    def test_chinese_paraphrase(self):
        # 同义但不同措辞仍应高相似(共享 贷款/次数 等特征)
        s = similarity("每年贷款次数", "每年发放的贷款笔数")
        assert s > 0.15

    def test_unrelated_is_low(self):
        s = similarity("哪个地区的平均贷款金额最高", "查询所有客户的出生日期")
        assert s < 0.2

    def test_embed_is_deterministic(self):
        assert embed("贷款金额") == embed("贷款金额")


class TestHybridRerank:
    def test_det_gate_preserved(self):
        """混合分只提升 det>0 的候选;det=0 仍应在调用侧被排除。"""
        assert rerank_score(0.0, 0.9) > 0.0
        # 门控语义由调用方(kb.service)的 `det <= 0: continue` 保证

    def test_sim_boosts_within_gate(self):
        low_sim = rerank_score(5, 0.1)
        high_sim = rerank_score(5, 0.9)
        assert high_sim > low_sim

    def test_weight_does_not_dominate_det_signal(self):
        # 高区分度 det 差(表锚)不被 embedding 单点覆盖:权重有限
        assert rerank_score(10, 0.0) > rerank_score(5, 0.99)


class TestCoverage:
    def test_covers_query_features(self):
        # 检索用相似度:候选覆盖问题特征的比例,不受候选长度影响
        q = "每年发放的贷款笔数"
        near = "每年贷款次数统计口径 按年份分组计数"
        far = "地区平均贷款金额 平均金额"
        assert coverage_score(q, near) > coverage_score(q, far)

    def test_unrelated_zero(self):
        assert coverage_score("zzz 无关问题", "地区平均贷款金额") == 0.0

    def test_empty_query(self):
        assert coverage_score("", "anything") == 0.0


class TestNearDuplicate:
    def test_identical_pattern(self):
        assert near_duplicate("no such table: loans", "no such table: loans")

    def test_unrelated_not_dup(self):
        assert not near_duplicate("no such table: loans", "地区平均贷款金额")

    def test_cosine_normalized(self):
        a = embed("每年贷款次数")
        assert abs(cosine(a, a) - 1.0) < 1e-6
