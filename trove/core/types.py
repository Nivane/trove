"""Core type definitions for the Trove system.

All shared data structures used across layers:
Message, Session, QueryResult, SchemaInfo, TableInfo, ColumnInfo,
and supporting types.

Workflow graph state lives in trove.workflow.state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# ── Task ─────────────────────────────────────────────────


@dataclass
class Task:
    """A cross-turn sub-task of a multi-part user instruction.

    Status flow (one-way chain with two side actions):
        pending → in_progress → done / failed
        skipped (user asked to skip) · redo (failed/done → pending again)
    """

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    title: str = ""  # 子问题原文(该任务的 question)
    status: Literal["pending", "in_progress", "done", "failed", "skipped"] = "pending"
    position: int = 0  # 列表顺序 0..n
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata may contain: run_id, sql, row_count, verdict, error, user_cancelled


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
    """Configuration for a single datasource connection.

    ``ds_id`` is the immutable identity of a datasource (UUID hex).
    ``name`` is a unique, user-facing handle. ``ds_id`` never changes
    across reconnects/re-registrations and is the persistence key that
    backs KB init locking; ``name`` remains the runtime/storage key the
    rest of the stack (workflow state, sessions, grants, KB directory)
    is keyed on. Empty ``ds_id`` is backfilled at registration time.
    """

    name: str
    type: str  # "sqlite", "duckdb", "postgres", "mysql", ...
    connection_params: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    default: bool = False
    ds_id: str = ""
    # 检索后端: "builtin"(确定性 + hashed n-gram,默认) | "hybrid"(FTS5
    # + BM25 稀疏通道) | "rag"(稀疏 + 稠密 embedding RRF)。读时生效,
    # 写 datasources.yml 即切换。
    retrieval_backend: str = "builtin"
    # 稠密通道 embedding 提供方:""/"api" → 经 LLM 网关的 GatewayEmbedder
    # (需凭证);"bge-m3" → 本地 BgeM3Embedder(FlagEmbedding,一个模型同时
    # 出 dense + sparse,无需凭证,推荐用于混合检索)。空 embedding_model
    # 时 rag 退化为纯稀疏,与 hybrid 同行为。
    embedder_backend: str = ""
    # rag 的稠密通道:embedding 模型名(经 LLM 网关,需凭证;空 → rag 退化
    # 纯稀疏,与 hybrid 同行为)。
    embedding_model: str = ""
    # 稀疏(learned-sparse)通道最大维度:bge-m3 lexical 词汇表约 250k;
    # 0 = 关闭 sparse 路(纯 keyword+dense 两路)。
    embedding_sparse_dims: int = 0
    # RRF 融合常数 k(标准 60):fuse = sum(weight / (k + rank))。
    rrf_k: int = 60
    # RRF 每路权重:{"keyword": .., "dense": .., "sparse": ..};缺省 = 等权 1.0。
    rrf_weights: dict[str, float] = field(default_factory=dict)
    # 精排后端:""/"auto"(端点→bge→cosine 近似→确定性) | "none"(跳过) |
    # "deterministic"(n-gram coverage) | "bge"(本地 FlagReranker) |
    # "http"(rerank_endpoint 的 Cohere/TEI 兼容 API) | "cross-encoder"
    # (embedder cosine 近似)。
    rerank_backend: str = ""
    # http 精排端点(Cohere/TEI 兼容 /rerank);留空且 rerank_backend="" 时
    # 按 auto 顺序选择。
    rerank_endpoint: str = ""
    # 向量后端: "pgvector"(默认,postgres 业务库同实例;vector_dsn 留空 =
    # 由业务库连接推导) | "sqlite"(kb.sqlite 本地向量,非 postgres 业务库回退)。
    vector_backend: str = "pgvector"
    # pgvector 向量库连接串(留空 = 与 postgres 业务库同实例推导;业务库非
    # postgres 时为空即退化为 sqlite 本地向量)。
    vector_dsn: str = ""
    # 统一 PostgreSQL 检索库连接串(混合检索 FTS+pgvector 的专属库;留空 =
    # 从 retrieval_dsn 推导的 postgres 业务库同实例;再无 → 退化为 SQLite 混合库)。
    retrieval_dsn: str = ""
    # 精排(cross-encoder)模型名;空 → 确定性 n-gram 精排(零 LLM)。
    rerank_model: str = ""
    # pgvector 向量维度:须与 embedding_model 实际输出维度一致(OpenAI text-embedding-3-small
    # = 1536,bge-m3 = 1024,m3e = 768 等);不一致会报向量长度错。留空默认 1536。
    embedding_dims: int = 1536
    # pg_bm25 分词器(仅混合检索 PG 后端):中文 KB 设 "chinese"(jieba),英文/通用设
    # "en_stem"(默认);ParadeDB 镜像内置 pg_bm25 扩展,本字段透传进 BM25 索引 WITH 子句。
    fts_tokenizer: str = "en_stem"
