"""Error hierarchy tests."""

import pytest

from trove.core.errors import (
    ErrorCode,
    TroveError,
    LLMError,
    SQLGenerationError,
    SQLExecutionError,
    DatasourceError,
    ConfigError,
    SessionError,
    CancelledError,
)


class TestTroveError:
    def test_basic_error(self):
        err = TroveError(code="SYS_001", message="config failed")
        assert err.code == "SYS_001"
        assert err.message == "config failed"
        assert err.recoverable is False
        assert err.details == {}

    def test_str_contains_code_and_message(self):
        err = TroveError(code="SQL_001", message="bad sql")
        assert "SQL_001" in str(err)
        assert "bad sql" in str(err)

    def test_error_with_details(self):
        err = TroveError(
            code="DS_001",
            message="connection failed",
            details={"host": "localhost"},
        )
        assert err.details["host"] == "localhost"

    def test_recoverable_flag(self):
        err = TroveError(code="X", recoverable=True)
        assert err.recoverable is True


class TestLLMError:
    def test_llm_error_fields(self):
        err = LLMError(
            message="provider down",
            provider="openai",
            model="gpt-4o",
            retry_after=5,
        )
        assert err.code == ErrorCode.LLM_PROVIDER_UNAVAILABLE
        assert err.provider == "openai"
        assert err.model == "gpt-4o"
        assert err.retry_after == 5
        assert err.recoverable is True


class TestSQLGenerationError:
    def test_with_validation_errors(self):
        err = SQLGenerationError(
            sql="SELCT * FROM t",
            validation_errors=["parse error"],
        )
        assert err.sql == "SELCT * FROM t"
        assert "parse error" in err.validation_errors
        assert err.recoverable is True


class TestSQLExecutionError:
    def test_with_db_error(self):
        err = SQLExecutionError(
            sql="SELECT * FROM missing",
            db_error="no such table: missing",
        )
        assert err.code == ErrorCode.SQL_TABLE_NOT_FOUND
        assert "no such table" in err.db_error
        assert err.recoverable is False


class TestOtherErrors:
    def test_datasource_error(self):
        err = DatasourceError(datasource="pg")
        assert err.code == ErrorCode.DS_CONNECTION_FAILED
        assert err.datasource == "pg"

    def test_config_error(self):
        err = ConfigError(message="bad yaml")
        assert err.code == ErrorCode.SYS_CONFIG_LOAD_FAILED

    def test_session_error(self):
        err = SessionError(session_id="abc")
        assert err.code == ErrorCode.CHAT_SESSION_NOT_FOUND
        assert err.session_id == "abc"

    def test_cancelled_error(self):
        err = CancelledError()
        assert "cancelled" in str(err).lower()


class TestErrorCodes:
    def test_codes_are_unique(self):
        """All error codes should be unique strings."""
        codes = [
            value
            for name, value in vars(ErrorCode).items()
            if name.startswith(("CHAT", "SQL", "KB", "DS", "AUTH", "MCP", "LLM", "SYS"))
        ]
        assert len(codes) == len(set(codes))

    def test_code_format(self):
        """Codes follow the {DOMAIN}_{NNN} pattern."""
        import re
        pattern = re.compile(r"^[A-Z]+_\d{3}$")
        for name, value in vars(ErrorCode).items():
            if not name.startswith("_"):
                assert pattern.match(value), f"{name}={value} does not match pattern"


class TestExceptionInheritance:
    def test_all_inherit_trove_error(self):
        for err_cls in [
            LLMError, SQLGenerationError, SQLExecutionError,
            DatasourceError, ConfigError, SessionError, CancelledError,
        ]:
            assert issubclass(err_cls, TroveError)

    def test_catch_as_base(self):
        with pytest.raises(TroveError):
            raise SQLExecutionError(sql="SELECT 1")
