"""Error classifier tests — deterministic ErrorClass + recovery matrix.

Covers the classify_error lexicon mapping (representative high-frequency
errors), context gating, retryability decisions (is_transient), the
[ERR:]-tagging contract, and lightweight tool-argument validation.
"""

import pymysql

import pytest

from trove.services.errors import (
    DETERMINISTIC_DEAD_END,
    RecoveryAction,
    classify_error,
    is_transient,
    tag_error,
    validate_arguments,
)


class TestClassifyLexicon:
    @pytest.mark.parametrize(
        ("text", "context", "expected"),
        [
            # ── SQL 层(数据 agent 最高频) ────────────────
            ("no such table: loans", "sql", "SQL_SCHEMA_MISSING"),
            ("execution failed: no such column: foo", "sql", "SQL_SCHEMA_MISSING"),
            ("sqlite: table 'district' not found", "sql", "SQL_SCHEMA_MISSING"),
            ("Unknown column 'a' in 'field list'", "sql", "SQL_SCHEMA_MISSING"),
            ("relation \"clients\" does not exist", "sql", "SQL_SCHEMA_MISSING"),
            ("you have an error in your SQL syntax near 'FROM'", "sql", "SQL_SYNTAX"),
            ("MySQL execution error: (1064, syntax error)", "sql", "SQL_SYNTAX"),
            ("sqlglot error: Error parsing input", "sql", "SQL_SYNTAX"),
            ("probe timed out after 5s", "sql", "SQL_TIMEOUT"),
            ("query timed out after 30000ms", "sql", "SQL_TIMEOUT"),
            ("write operations are not permitted under NORMAL", "sql", "SQL_WRITEOP"),
            ("operation is not allowed by read-only guard", "sql", "SQL_PERMISSION"),
            ("permission denied for relation loans", "sql", "SQL_PERMISSION"),
            ("Trove is read-only: write operation INSERT is not allowed", "sql", "SQL_WRITEOP"),
            ("only SELECT queries are allowed (rejected: Block)", "sql", "SQL_WRITEOP"),
            ("table sqlite_master is a metadata/system table", "sql", "SQL_WRITEOP"),
            ("table students is not in the allowed tables", "sql", "SQL_WRITEOP"),
            ("SELECT INTO OUTFILE is not allowed (writes a file)", "sql", "SQL_WRITEOP"),
            ("type mismatch: cannot cast TEXT to INTEGER", "sql", "SQL_EXEC_TYPE"),
            ("No function matches the given name", "sql", "SQL_EXEC_TYPE"),
            ("operator does not exist: integer = text", "sql", "SQL_EXEC_TYPE"),
            # ── 数据源层 ──────────────────────────────────
            ("Lost connection to MySQL server", "sql", "DS_TRANSIENT"),
            ("Too many connections", "sql", "RATE_LIMIT"),
            ("Access denied for user 'u'@'host'", "sql", "DS_AUTH"),
            ("password authentication failed for user", "sql", "DS_AUTH"),
            # ── LLM 层(上下文门控) ────────────────────────
            ("status code 429: rate limit", "llm", "LLM_TRANSIENT"),
            ("connection timed out to provider", "llm", "LLM_TRANSIENT"),
            ("401 unauthorized: invalid api_key", "llm", "LLM_SERVICE"),
            ("model not found: openai/xyz", "llm", "LLM_SERVICE"),
            ("context length exceeded (max 128k)", "llm", "LLM_SERVICE"),
            # ── 语义性错误不落入词典(交给 LLM) ────────────
            ("Validation rule [F1-b]: returned wide columns", "workflow", "UNKNOWN"),
            ("candidate SQLs disagreed on rows", "workflow", "UNKNOWN"),
            ("boom", "workflow", "UNKNOWN"),
        ],
    )
    def test_lexicon_mapping(self, text, context, expected):
        verdict = classify_error(text, context=context)
        assert verdict.cls.id == expected, (text, verdict.cls.id)

    def test_context_gating_prevents_cross_layer_misfire(self):
        # "model not found" 只在 LLM 上下文被判为服务错误;SQL 语境不被误伤
        assert classify_error("model not found", context="llm").cls.id == "LLM_SERVICE"
        assert classify_error("model not found", context="sql").cls.id == "UNKNOWN"
        # "404" 裸数字只在 LLM 上下文有意义
        assert classify_error("404", context="llm").cls.id == "LLM_SERVICE"
        assert classify_error("404", context="sql").cls.id == "UNKNOWN"

    def test_http_status_type_signal(self):
        class Fake429(Exception):
            status_code = 429

        verdict = classify_error("", exc=Fake429(), context="llm")
        assert verdict.cls.id == "LLM_TRANSIENT"
        assert verdict.retryable is True

        class Fake403(Exception):
            status_code = 403

        verdict = classify_error("", exc=Fake403(), context="llm")
        assert verdict.cls.id == "LLM_SERVICE"
        assert verdict.retryable is False

    def test_timeout_by_context(self):
        import asyncio

        assert classify_error("", exc=asyncio.TimeoutError(), context="llm").cls.id == "LLM_TRANSIENT"
        assert classify_error("", exc=TimeoutError(), context="sql").cls.id == "SQL_TIMEOUT"
        assert classify_error("", exc=TimeoutError(), context="tool").cls.id == "TOOL_TIMEOUT"


class TestRetryability:
    def test_is_transient_connection_recovery(self):
        # 与 execute_sql 旧 _is_transient 语义对等(类型 + 文本双信号)
        assert is_transient(pymysql.err.OperationalError(1040, "Too many connections"))
        assert is_transient(pymysql.err.InterfaceError(0, ""))
        assert is_transient(Exception("Lost connection to MySQL server"))
        # SQL 错误重跑必死 → 绝不当瞬态
        assert not is_transient(Exception("no such table: foo"))
        assert not is_transient(Exception("you have an error in your SQL syntax"))
        assert not is_transient(Exception("Access denied for user 'x'"))

    def test_permanent_errors_not_retryable(self):
        for text, ctx in [
            ("Access denied for user", "sql"),
            ("401 unauthorized", "llm"),
            ("permission denied for relation loans", "sql"),
        ]:
            assert classify_error(text, context=ctx).retryable is False

    def test_guard_writeop_is_retryable(self):
        """guard 拦截的自产写操作可修复重试(重写为只读 SELECT),不是死胡同。"""
        assert classify_error("write operations are not permitted", context="sql").cls.id == "SQL_WRITEOP"
        assert classify_error("write operations are not permitted", context="sql").retryable is True

    def test_unknown_defaults_to_retryable_and_analyze(self):
        """词典覆盖不到的失败默认可重试 + 交 LLM 分析,绝不静默吞掉。"""
        verdict = classify_error("weird failure", context="workflow")
        assert verdict.cls.id == "UNKNOWN"
        assert verdict.retryable is True
        assert verdict.recovery == RecoveryAction.ANALYZE


class TestTagging:
    def test_tag_error_prefixes_machine_class(self):
        assert tag_error("no such table: loans", context="sql") == (
            "[ERR:SQL_SCHEMA_MISSING] no such table: loans"
        )

    def test_tag_error_is_idempotent(self):
        tagged = tag_error("no such table: loans", context="sql")
        assert tag_error(tagged, context="sql") == tagged

    def test_tag_error_passthrough_when_unknown(self):
        msg = "inexplicable failure"
        assert tag_error(msg, context="sql") == msg

    def test_dead_end_set_contains_surfaceable_classes(self):
        assert {"SQL_PERMISSION", "DS_AUTH", "LLM_SERVICE", "TOOL_RUNTIME",
                "ARGS_SCHEMA"} <= DETERMINISTIC_DEAD_END


class TestValidateArguments:
    PARAMS = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "the SQL"},
            "limit": {"type": "integer"},
            "mode": {"type": "string", "enum": ["dry", "live"]},
        },
        "required": ["sql"],
    }

    def test_missing_required(self):
        problems = validate_arguments(self.PARAMS, {"limit": 5})
        assert problems == ["missing required field 'sql'"]

    def test_valid_args_pass(self):
        assert validate_arguments(self.PARAMS, {"sql": "SELECT 1", "limit": 5, "mode": "dry"}) == []

    def test_wrong_type(self):
        problems = validate_arguments(self.PARAMS, {"sql": "SELECT 1", "limit": "abc"})
        assert "must be of type integer" in problems[0]

    def test_enum_violation(self):
        problems = validate_arguments(self.PARAMS, {"sql": "SELECT 1", "mode": "explode"})
        assert "must be one of" in problems[0]

    def test_absent_params_are_lenient(self):
        assert validate_arguments(None, {}) == []
        assert validate_arguments({}, {"anything": 1}) == []