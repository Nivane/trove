"""输入解析:编码/表头/去重/上限 —— 丢掉什么必须说得出来。

体检分母的可信度从这里开始:一次静默丢行,覆盖率就悄悄变了。
"""

from __future__ import annotations

import pytest

from trove.services.question_coverage.csv_input import (
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS,
    CsvInputError,
    dedupe_key,
    normalize_question,
    parse_csv,
    parse_pasted,
)


def test_utf8_bom_and_header_autodetect():
    data = "﻿Question,owner\nhow many loans?,a\n".encode()
    parsed = parse_csv(data)
    assert parsed.questions == ["how many loans?"]
    assert parsed.header == "Question"
    assert parsed.encoding == "utf-8-sig"
    assert parsed.duplicates == 0


def test_gb18030_fallback():
    data = "问题\n贷款总额是多少\n".encode("gb18030")
    parsed = parse_csv(data)
    assert parsed.questions == ["贷款总额是多少"]
    assert parsed.encoding == "gb18030"
    assert parsed.header == "问题"


def test_unknown_header_takes_first_column_and_warns():
    data = b"name,amount\nq1,1\nq2,2\n"
    parsed = parse_csv(data)
    assert parsed.questions == ["name", "q1", "q2"]
    assert parsed.header == ""
    assert "header_not_found" in parsed.warnings


def test_explicit_column_by_name_and_by_index():
    data = b"id,question_text\n1,q one\n2,q two\n"
    by_name = parse_csv(data, question_column="QUESTION_TEXT")
    assert by_name.questions == ["q one", "q two"]
    # 数字列号是 1 起用户口径,且不假定首行是表头。
    by_index = parse_csv(data, question_column="2")
    assert by_index.questions == ["q one", "q two"]
    assert by_index.header == "question_text"


def test_missing_explicit_column_is_loud():
    with pytest.raises(CsvInputError) as exc:
        parse_csv(b"a,b\nq1,1\n", question_column="question")
    assert exc.value.code == "question_column_not_found"


def test_blank_rows_and_duplicates_are_counted():
    data = b"question\nq1\n\nq1\n  q1  \nq2\n,\n"
    parsed = parse_csv(data)
    assert parsed.questions == ["q1", "q2"]
    assert parsed.duplicates == 2
    assert parsed.skipped_blank == 2


def test_short_rows_are_treated_as_blank_not_crashing():
    data = b"question,extra\nq1,x\n\nonly-one-cell\n"
    parsed = parse_csv(data)
    assert parsed.questions == ["q1", "only-one-cell"]


def test_too_long_questions_are_rejected_per_row_with_issue():
    long_q = "x" * (MAX_QUESTION_CHARS + 1)
    parsed = parse_csv(("question\n" + long_q + "\nok\n").encode())
    assert parsed.questions == ["ok"]
    assert parsed.skipped_long == 1
    assert parsed.issues[0]["reason"] == "question_too_long"


def test_issue_list_is_bounded():
    rows = "\n".join("y" * (MAX_QUESTION_CHARS + 1) for _ in range(25))
    parsed = parse_csv(("question\n" + rows + "\n").encode())
    assert len(parsed.issues) == 20
    assert parsed.issues_truncated is True
    assert parsed.questions == []


def test_question_cap_truncates_with_warning():
    rows = "\n".join(f"q{i}" for i in range(MAX_QUESTIONS + 5))
    parsed = parse_csv(("question\n" + rows + "\n").encode())
    assert len(parsed.questions) == MAX_QUESTIONS
    assert parsed.truncated is True
    assert "question_cap" in parsed.warnings


def test_scan_cap_is_reported():
    # 全重复行:不会先撞 200 题上限,扫描上限才有机会显形。
    rows = "\n".join("dup" for _ in range(2001))
    parsed = parse_csv(("question\n" + rows + "\n").encode())
    assert "scan_capped" in parsed.warnings
    assert parsed.rows_seen == 2000
    assert parsed.duplicates == 1999


def test_hard_input_errors():
    with pytest.raises(CsvInputError) as exc:
        parse_csv(b"")
    assert exc.value.code == "empty_file"

    with pytest.raises(CsvInputError) as exc:
        parse_csv(b"a" * (1_048_576 + 1))
    assert exc.value.code == "file_too_large"

    with pytest.raises(CsvInputError) as exc:
        parse_csv(b"question\nbad\x00cell\n")
    assert exc.value.code == "invalid_csv"

    with pytest.raises(CsvInputError) as exc:
        parse_csv(b'question\n"unclosed\nnext\n')
    assert exc.value.code == "invalid_csv"


def test_replacement_decoding_is_flagged():
    data = "问题\nbad ".encode() + b"\xff" + b" byte\n"
    parsed = parse_csv(data)
    assert "encoding_replaced" in parsed.warnings
    assert parsed.questions  # 一个坏字节不该让整张表不可用


def test_paste_normalises_and_dedupes_like_csv():
    parsed = parse_pasted("How   many\tloans?\n\n how many loans? \n第二题\n")
    assert parsed.questions == ["How many loans?", "第二题"]
    assert parsed.duplicates == 1
    assert parsed.skipped_blank == 1
    assert parsed.encoding == "text"


def test_normalize_and_key_are_the_same_function_used_by_taxonomy():
    assert normalize_question("  a \n b  ") == "a b"
    assert dedupe_key("A  B") == dedupe_key("a b")
    assert normalize_question(None) == ""
