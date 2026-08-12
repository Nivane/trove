"""Error taxonomy and error codes for Trove.

All system errors inherit from TroveError which carries
a machine-readable error code and human-readable message.
"""

from __future__ import annotations

from typing import Any


# ── Error Code Constants ─────────────────────────────────

class ErrorCode:
    """Machine-readable error codes: {DOMAIN}_{NNN}"""

    # Chat / Session
    CHAT_SESSION_NOT_FOUND = "CHAT_001"
    CHAT_INVALID_MESSAGE = "CHAT_002"
    CHAT_COMPACT_FAILED = "CHAT_003"

    # SQL
    SQL_INVALID_SYNTAX = "SQL_001"
    SQL_DIALECT_INCOMPATIBLE = "SQL_002"
    SQL_TABLE_NOT_FOUND = "SQL_003"
    SQL_EXECUTION_TIMEOUT = "SQL_004"
    SQL_PERMISSION_DENIED = "SQL_005"

    # Knowledge Base
    KB_NOT_BUILT = "KB_001"
    KB_BUILD_INTERRUPTED = "KB_002"
    KB_EMBED_MODEL_UNAVAILABLE = "KB_003"

    # Datasource
    DS_CONNECTION_FAILED = "DS_001"
    DS_ALREADY_EXISTS = "DS_002"
    DS_UNSUPPORTED_TYPE = "DS_003"

    # Auth
    AUTH_USER_ID_MISSING = "AUTH_001"
    AUTH_CONFIG_IMMUTABLE = "AUTH_002"

    # MCP
    MCP_SERVER_UNREACHABLE = "MCP_001"
    MCP_TOOL_CALL_FAILED = "MCP_002"
    MCP_INVALID_FILTER = "MCP_003"

    # LLM
    LLM_PROVIDER_UNAVAILABLE = "LLM_001"
    LLM_MODEL_NOT_FOUND = "LLM_002"
    LLM_TOKEN_LIMIT = "LLM_003"

    # System
    SYS_CONFIG_LOAD_FAILED = "SYS_001"
    SYS_INTERNAL = "SYS_002"


# ── Exception Hierarchy ──────────────────────────────────


class TroveError(Exception):
    """Base error for all Trove exceptions.

    Attributes:
        code: Machine-readable error code (e.g. "SQL_001").
        message: Human-readable description.
        recoverable: Whether the system can attempt recovery.
        details: Optional extra context for debugging.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        recoverable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.details = details or {}
        super().__init__(message or code)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        return (
            f"TroveError(code={self.code!r}, message={self.message!r}, "
            f"recoverable={self.recoverable})"
        )


class LLMError(TroveError):
    """LLM call failed."""

    def __init__(
        self,
        message: str = "LLM provider unavailable",
        provider: str = "",
        model: str = "",
        retry_after: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            code=ErrorCode.LLM_PROVIDER_UNAVAILABLE,
            message=message,
            recoverable=True,
            details=details or {},
        )
        self.provider = provider
        self.model = model
        self.retry_after = retry_after


class SQLGenerationError(TroveError):
    """SQL generation or validation failed."""

    def __init__(
        self,
        message: str = "SQL generation failed",
        sql: str | None = None,
        validation_errors: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            code=ErrorCode.SQL_INVALID_SYNTAX,
            message=message,
            recoverable=True,
            details=details or {},
        )
        self.sql = sql
        self.validation_errors = validation_errors or []


class SQLExecutionError(TroveError):
    """SQL execution failed at the database level."""

    def __init__(
        self,
        message: str = "SQL execution failed",
        sql: str = "",
        db_error: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            code=ErrorCode.SQL_TABLE_NOT_FOUND,
            message=message,
            recoverable=False,
            details=details or {},
        )
        self.sql = sql
        self.db_error = db_error


class DatasourceError(TroveError):
    """Datasource connection or operation failed."""

    def __init__(
        self,
        message: str = "Datasource connection failed",
        datasource: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            code=ErrorCode.DS_CONNECTION_FAILED,
            message=message,
            recoverable=False,
            details=details or {},
        )
        self.datasource = datasource


class ConfigError(TroveError):
    """Configuration loading or validation failed."""

    def __init__(
        self,
        message: str = "Configuration error",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            code=ErrorCode.SYS_CONFIG_LOAD_FAILED,
            message=message,
            recoverable=False,
            details=details or {},
        )


class SessionError(TroveError):
    """Session operation failed."""

    def __init__(
        self,
        message: str = "Session error",
        session_id: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            code=ErrorCode.CHAT_SESSION_NOT_FOUND,
            message=message,
            recoverable=False,
            details=details or {},
        )
        self.session_id = session_id


class CancelledError(TroveError):
    """Workflow or query was cancelled by user."""

    def __init__(self, message: str = "Operation cancelled by user"):
        super().__init__(
            code=ErrorCode.SYS_INTERNAL,
            message=message,
            recoverable=False,
        )
