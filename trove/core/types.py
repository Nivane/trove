"""Core type definitions for the Trove system.

All shared data structures used across layers:
Message, Session, WorkflowContext, NodeResult, QueryResult,
SchemaInfo, TableInfo, ColumnInfo, and supporting types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal


# ── Message ──────────────────────────────────────────────


@dataclass
class Message:
    """A single message in a conversation."""

    role: Literal["user", "assistant", "system", "tool"]
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata may contain: trace_id, sql_generated, token_usage, tool_calls, ...


# ── Session ──────────────────────────────────────────────


@dataclass
class Session:
    """A conversation session persisted per-project."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_name: str = "default"
    user_id: str = "local"
    messages: list[Message] = field(default_factory=list)
    summary: str | None = None  # recap after compaction
    branch_parent: str | None = None  # /rewind branch source session_id
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata may contain: model, datasource, language, workflow, ...


# ── Workflow ─────────────────────────────────────────────


class NodeStatus(Enum):
    SUCCESS = "success"
    ERROR = "error"
    RETRY = "retry"
    SKIP = "skip"


@dataclass
class NodeResult:
    """Result produced by a single workflow node."""

    node_name: str
    status: NodeStatus
    data: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata: elapsed_ms, token_usage, tool_calls_count, ...


@dataclass
class WorkflowResult:
    """Aggregated result from a full workflow run."""

    workflow_name: str
    nodes: list[NodeResult] = field(default_factory=list)
    final_output: str = ""
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token_usage: dict[str, int] = field(default_factory=dict)
    # token_usage: {"prompt": N, "completion": N, "total": N}


@dataclass
class WorkflowContext:
    """Context carried through a single workflow execution."""

    session: Session
    user_message: Message
    config: Any  # AgentConfig — forward ref to avoid circular import
    kb_hits: list[Any] = field(default_factory=list)  # KBHit[]
    injected_context: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancellation_event: Any = None  # asyncio.Event, set after import

    def __post_init__(self):
        import asyncio
        if self.cancellation_event is None:
            self.cancellation_event = asyncio.Event()


# ── Datasource / Schema ──────────────────────────────────


@dataclass
class ColumnInfo:
    """Metadata for a single table column."""

    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    foreign_key: str | None = None  # "other_table.other_column"


@dataclass
class TableInfo:
    """Metadata for a single table."""

    name: str
    schema: str = "main"
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count_estimate: int | None = None


@dataclass
class SchemaInfo:
    """Full schema information for a datasource."""

    tables: list[TableInfo] = field(default_factory=list)


@dataclass
class QueryResult:
    """Unified query result from any datasource adapter."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    execution_time_ms: float = 0.0
    sql: str = ""
    datasource: str = ""


@dataclass
class Capabilities:
    """Probed capabilities of a database connection."""

    supports_cte: bool = True
    supports_window_functions: bool = True
    supports_transactions: bool = True
    supports_json_type: bool = False
    dialect: str = ""


# ── Datasource Config ────────────────────────────────────


@dataclass
class DatasourceConfig:
    """Configuration for a single datasource connection."""

    name: str
    type: str  # "sqlite", "duckdb", "postgres", "mysql", ...
    connection_params: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    default: bool = False
