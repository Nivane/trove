"""lint_stats tests — profiling stats 格式校验。"""

from trove.services.kb.lint import lint_stats


def test_valid_stats_no_issues():
    tables = [{"name": "account", "stats": {
        "frequency": {"null_ratio": 0.0, "distinct": 4, "shape": "all_caps"},
        "date": {"min": "1993-01-01", "max": "1998-12-31"},
        "amount": {"null_ratio": 0.5, "min_len": 1, "max_len": 12},
    }}]
    assert lint_stats(tables) == []


def test_unknown_key_flagged():
    tables = [{"name": "t", "stats": {"c": {"minhash": "abc"}}}]
    issues = lint_stats(tables)
    assert any("未知键" in i for i in issues)


def test_null_ratio_out_of_range():
    tables = [{"name": "t", "stats": {"c": {"null_ratio": 1.5}}}]
    issues = lint_stats(tables)
    assert any("null_ratio 超出" in i for i in issues)


def test_distinct_must_be_non_negative_int():
    tables = [{"name": "t", "stats": {"c": {"distinct": -1}}}]
    assert lint_stats(tables)
    tables = [{"name": "t", "stats": {"c": {"distinct": "many"}}}]
    assert lint_stats(tables)


def test_min_len_greater_than_max_len():
    tables = [{"name": "t", "stats": {"c": {"min_len": 10, "max_len": 5}}}]
    issues = lint_stats(tables)
    assert any("min_len > max_len" in i for i in issues)


def test_unknown_shape_flagged():
    tables = [{"name": "t", "stats": {"c": {"shape": "hexadecimal"}}}]
    issues = lint_stats(tables)
    assert any("shape 未知" in i for i in issues)


def test_stats_not_a_dict():
    tables = [{"name": "t", "stats": {"c": "fast"}}]
    issues = lint_stats(tables)
    assert any("不是对象" in i for i in issues)


def test_missing_stats_section_ok():
    assert lint_stats([{"name": "t"}]) == []
