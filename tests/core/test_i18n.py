"""Language detection tests — zh/en adaptive output."""

from trove.core.i18n import detect_language


class TestDetectLanguage:
    def test_chinese(self):
        assert detect_language("哪个地区的平均贷款金额最高?") == "zh"
        assert detect_language("loan 表的字段有哪些") == "zh"  # 含中文 → zh

    def test_english(self):
        assert detect_language("how many accounts have loans") == "en"
        assert detect_language("list tables") == "en"

    def test_empty_defaults_to_en(self):
        assert detect_language("") == "en"
