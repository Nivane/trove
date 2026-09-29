"""Core type definitions for the Trove system.

All shared data structures used across layers:
Message, Session, QueryResult, SchemaInfo, TableInfo, ColumnInfo,
and supporting types.

Workflow graph state lives in trove.workflow.state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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


# ── 执行画像(execution profile)───────────────────────────
#
# 放在这里而不是 ``services/datasource/profile.py``:``TableProfile`` 是
# **适配器的返回类型**,与 ``TableInfo`` / ``Capabilities`` 同级。落在服务层会让
# ``adapters/base.py`` 反向依赖上层模块 —— 依赖方向颠倒,而且第一个想在下层复用
# 它的人会踩坑。``profile.py`` 仍然 re-export,设计 §7.1 的导入路径不变。


@dataclass(frozen=True)
class TableProfile:
    """表级画像。**任何字段不可得时为 ``None``** —— 不可得 ≠ 0(§6.1)。

    ``capabilities`` 说的是**这个适配器实现了哪些字段的采集**,不是「这一行恰好
    有值」:某张表统计信息没收集时 ``row_count`` 是 ``None``,而 ``"row_count"``
    仍在集合里。反过来会让「这张表缺统计」被误诊成「这个库不支持行数」,把运维
    引到错误的修法上(§8.2 B)。
    """

    table: str
    #: 行数。归一入口是既有 ``TableInfo.row_count_estimate``
    row_count: int | None = None
    bytes: int | None = None
    #: ISO8601;数据新鲜度的核心字段
    last_modified: str | None = None
    last_analyzed: str | None = None
    partition_column: str | None = None
    partition_count: int | None = None
    #: 最新分区值(判断数据截止)
    latest_partition: str | None = None
    #: 本适配器**实际实现**的字段子集(§8.2 B)
    capabilities: frozenset[str] = frozenset()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


def positive_int(value: Any) -> int | None:
    """正值 → int;``None`` / 0 / 负数 / 非数 → ``None``(**没有依据**)。

    **0 不是依据。** 画像返回 0 最可能的原因是统计信息没收集,而不是「这张表确实
    是空的」;当成 0 会让一条扫全表的查询被判成零成本 —— 方向恰好是**放宽**,而
    这正是成本轨要修的方向。

    ``None`` / 0 语义一致化的地方不止一处(``R1``:成本轨估算、画像求和、
    适配器归一),**三处用同一条判据**,所以它在这里而不是任一侧 —— 各存一份的
    版本迟早在其中一处被「顺手修好」,而不变量就那样没了。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def timestamp_str(value: Any) -> str | None:
    """时间戳 → ISO 字符串;不可得 → ``None``(同 ``positive_int`` 的时间形式)。

    两个坑都属于「用某个字面量冒充了一个值」:

    * **MySQL 的零日期** ``0000-00-00 00:00:00`` 是它表示「没设置」的写法。
      放过去会流进 ``freshness`` 的 ``min()``,把整片数据的截止时间拉到公元 0 年
      —— 而且格式正确、看不出来。
    * **驱动给的是对象不是字符串**:aiomysql / psycopg 对 DATETIME 列返回
      ``datetime``。直接塞进 ``as_of: str`` 会得到 repr,比 ``None`` 更糟 ——
      它长得像有值。

    引擎给的**字符串原样带出**:覆盖各家方言的格式要写解析器,猜错的代价是谎报
    一个更精确的时间;原样带出不会比真实值更新(比较方向见 ``freshness``)。
    """
    if isinstance(value, date):  # datetime 是 date 的子类,一起命中
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0000-00-00"):
        return None
    return text


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


# ── 只读角色自检(设计 §4 I1)──────────────────────────────
#
# 同 ``TableProfile`` 的理由放在这里:它是**适配器的返回类型**,落在服务层会让
# ``adapters/base.py`` 反向依赖上层。

#: ``ReadonlyProbe.basis`` 的四个取值。**为什么不是「一个布尔」**:I1 要断言的
#: 是一道硬边界**确实存在**,而「不知道」与「确认只读」在布尔里同形 —— 把前者
#: 说成后者,等于替一道并不存在的边界背书(与 ``as_of_basis`` / ``kill`` 同一
#: 套三态纪律)。四个值各自对应一种**不同的修法**,所以它们不能合并:
#:
#: * ``grants``       查了权限表,有正面依据 —— 唯一能支撑结论的 basis
#: * ``unverifiable`` 这个方言根本没有角色概念(SQLite / DuckDB 是文件权限)
#: * ``probe_failed`` 查了,没查成(权限不足 / 超时 / 连接断)
#: * ``not_probed``   启动自检时还没有这个源(admin 后注册的)
BASIS_GRANTS = "grants"
BASIS_UNVERIFIABLE = "unverifiable"
BASIS_PROBE_FAILED = "probe_failed"
BASIS_NOT_PROBED = "not_probed"


@dataclass(frozen=True)
class ReadonlyProbe:
    """数据源账号是否**确实只能读**(设计 §4 I1)。

    ``verified`` 是三值而不是布尔:``True`` = 有正面证据表明写不了,
    ``False`` = 有正面证据表明**写得动**(硬边界不存在,该 WARN),
    ``None`` = 没查成 / 这个方言查不了。看 ``basis`` 才知道是哪种。
    """

    #: True / False / None(不知道)—— None 不是「安全」
    verified: bool | None
    basis: str
    #: 人话依据,**只进日志**。不进健康检查响应,见 :meth:`to_health`。
    detail: str = ""

    def to_health(self) -> dict:
        """健康检查里的形状。``detail`` **刻意不在这里**。

        健康检查的既有纪律是「错误只报类型名,不回传驱动原文」(避免凭据/主机
        信息入响应);而这个 detail 还会带上库里的对象名与授权原文。
        """
        return {"verified": self.verified, "basis": self.basis}


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
    # (需凭证);"bge-m3" → 本地 BgeM3Embedder(FlagEmbedding,无需凭证,推荐
    # 用于混合检索)。空 embedding_model 时 rag 退化为纯稀疏,与 hybrid 同行为。
    embedder_backend: str = ""
    # rag 的稠密通道:embedding 模型名(经 LLM 网关,需凭证;空 → rag 退化
    # 纯稀疏,与 hybrid 同行为)。
    embedding_model: str = ""
    # RRF 融合常数 k(标准 60):fuse = sum(weight / (k + rank))。
    rrf_k: int = 60
    # RRF 每路权重:{"keyword": .., "dense": ..};缺省 = 等权 1.0。
    rrf_weights: dict[str, float] = field(default_factory=dict)
    # 精排后端:**""/"auto"/"none" = 不精排(默认)** —— 默认档曾是确定性
    # n-gram coverage,与下游 _rank_examples 的 _sim 同函数同源,融合等于原值,
    # 纯重复计算。显式配 | "deterministic"(n-gram coverage) | "bge"(本地
    # FlagReranker) | "http"(rerank_endpoint 的 Cohere/TEI 兼容 API) |
    # "cross-encoder"(有端点走端点,否则 embedder cosine 近似)。
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
    # pgvector HNSW 索引参数(仅混合检索 PG 后端):m(每节点最大连接数)与
    # ef_construction(建索引贪心搜索宽度)在索引创建时生效;ef_search 为查询时
    # 的候选队列宽度(越大召回越高、越慢)。0/空 = pgvector 默认(m=16,
    # ef_construction=64,ef_search=40)。任一项配置后,每次进程启动的 _ensure
    # 会重建向量索引以应用新参数(与 pg_bm25 索引的 DROP+CREATE 同策略)。
    hnsw_m: int = 0
    hnsw_ef_construction: int = 0
    hnsw_ef_search: int = 0
    # 表级授权白名单(执行期):非空时,Trove 只允许查询这些业务表(小写,
    # 大小写不敏感);空 = 不限制(仅保持元数据表拒绝)。持久化在
    # datasources.yml,由管理端在注册时配置;作为执行层纵深授权,与生成层
    # 的 allowlist 相互印证。真正的权限边界仍在数据库侧只读角色。
    allowed_tables: list[str] = field(default_factory=list)
