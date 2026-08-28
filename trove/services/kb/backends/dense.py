"""Dense embedding + vector storage — the dense channel of the RAG backend.

依赖分层:
- Embedder:文本 → 向量(生产走 LLMGateway.embedding;测试注入确定性 fake)。
- VectorStore:向量持久化 + 余弦近邻查询。SQLite 本地向量表是默认零配置
  实现(kb.sqlite 的 kb_vectors 镜像);PgVectorStore 是 opt-in 的 Postgres
  pgvector 后端(独立 vector_dsn),按数据源配置切换。

约束:
- 索引内容只来自 KB 语义文件(fts_item_text 的 text)——不索引物理 schema,
  语义优先边界不被向量库绕过。
- 查询失败降级:调用方(RagBackend)对 dense 异常退化到稀疏通道。
"""

from __future__ import annotations

import math
import struct
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import aiosqlite

from trove.core.logging import get_logger

logger = get_logger(__name__)

# SQLite 本地向量镜像表(rowid 对齐 kb_items.id;随 _sync_file 重建)。
CREATE_VECTORS = """CREATE TABLE IF NOT EXISTS kb_vectors (
    id INTEGER PRIMARY KEY,
    datasource TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL,
    vector BLOB NOT NULL
)"""


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def _unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """两向量余弦相似度(0~1,空/零向量 → 0)。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@runtime_checkable
class Embedder(Protocol):
    """文本 → 向量(每输入一个,长度 = 模型维度)。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class GatewayEmbedder:
    """生产 Embedder:走 LLMGateway.embedding(LLM 凭证,opt-in)。"""

    def __init__(self, gateway: Any, model: str, batch_size: int = 32):
        self._gateway = gateway
        self._model = model
        self._batch_size = max(1, batch_size)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i:i + self._batch_size]
            out.extend(await self._gateway.embedding(self._model, batch))
        return out


class BgeM3Embedder:
    """本地 learned-sparse Embedder:FlagEmbedding 的 bge-m3,一个模型同时出
    dense + sparse(lexical 权重)。

    稀疏通道的"业内更强做法"(learned sparse,替代纯 BM25):bge-m3 的 lexical
    权重是学出来的词表加权,兼有词面匹配与语义相关。模型本地运行,无需 LLM
    凭证(依赖 ``uv sync --extra bge``)。dense/sparse 各一次 encode 会重复计算,
    故提供 ``embed_hybrid`` 一次出两路,store 的索引/召回都优先走它。
    """

    def __init__(self, model: str = "BAAI/bge-m3", sparse_dim: int = 250000):
        self._model = model
        self._sparse_dim = int(sparse_dim or 250000)
        self._backend: Any = None

    def _load(self) -> Any:
        if self._backend is None:
            try:
                from FlagEmbedding import BGEM3FlagModel
            except ImportError as e:  # pragma: no cover - 依赖提示
                raise RuntimeError(
                    "BgeM3Embedder requires 'FlagEmbedding' "
                    "(uv sync --extra bge)") from e
            self._backend = BGEM3FlagModel(self._model, use_fp16=False)
        return self._backend

    def _clip(self, weights: dict) -> dict[int, float]:
        """lexical 权重 → {int 词表 id: float},维度上限裁剪。"""
        out: dict[int, float] = {}
        for k, v in weights.items():
            i = int(k)
            if i < self._sparse_dim:
                out[i] = float(v)
        return out

    async def embed_hybrid(
        self, texts: list[str],
    ) -> list[tuple[list[float], dict[int, float]]]:
        out = self._load().encode(texts, return_dense=True, return_sparse=True)
        dense = [list(map(float, v)) for v in out["dense_vecs"]]
        sparse = [self._clip(lw) for lw in out["lexical_weights"]]
        return list(zip(dense, sparse))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [d for d, _ in await self.embed_hybrid(texts)]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return [s for _, s in await self.embed_hybrid(texts)]


def sparse_supported(embedder: Any) -> bool:
    """embedder 是否提供 learned-sparse 通道(embed_sparse / embed_hybrid)。"""
    return embedder is not None and (
        hasattr(embedder, "embed_sparse") or hasattr(embedder, "embed_hybrid"))


def build_embedder(cfg: Any, gateway: Any) -> Any:
    """按数据源配置构造稠密/稀疏 Embedder(与 factory / registry 共享)。

    - ``embedder_backend`` in {"bge-m3", "bge"} → 本地 BgeM3Embedder
      (dense + sparse,无需凭证;稀疏维数取 embedding_sparse_dims)。
    - 否则 ``embedding_model`` 非空 → GatewayEmbedder(LLM 网关)。
    - 空 → None(纯稀疏/退化路径)。
    """
    backend = str(getattr(cfg, "embedder_backend", "") or "").strip().lower()
    model = str(getattr(cfg, "embedding_model", "") or "").strip()
    sparse_dim = int(getattr(cfg, "embedding_sparse_dims", 0) or 0)
    if backend in ("bge-m3", "bge"):
        return BgeM3Embedder(model=model or "BAAI/bge-m3", sparse_dim=sparse_dim)
    if model:
        return GatewayEmbedder(gateway, model)
    return None


@runtime_checkable
class VectorStore(Protocol):
    """向量持久化 + 近邻查询接口(按数据源隔离)。"""

    async def replace(self, datasource: str, source_file: str,
                      items: list[tuple[int, str, list[float]]]) -> None:
        """幂等重建一个文件的向量:删除旧行 + 插入新行(同批次)。"""

    async def delete_file(self, datasource: str, source_file: str) -> None:
        """删除一个文件的向量(删除传播)。"""

    async def clear(self, datasource: str) -> None:
        """清空一个数据源的全部向量(delete_kb)。"""

    async def query(
        self, datasource: str, vector: list[float], kinds: tuple[str, ...], limit: int,
    ) -> list[tuple[int, float]]:
        """余弦近邻 top-k:返回 [(kb_items.id, sim)]。"""


class SqliteVectorStore:
    """默认向量后端:kb.sqlite 的 kb_vectors 表,Python 余弦(规模 = 单源 KB)。"""

    def __init__(self, kb: Any):
        self._kb = kb

    @property
    def db_path(self) -> str:
        return str(self._kb.db_path)

    async def replace(self, datasource: str, source_file: str,
                      items: list[tuple[int, str, list[float]]]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_VECTORS)
            await db.execute(
                "DELETE FROM kb_vectors WHERE datasource = ? AND source_file = ?",
                (datasource, source_file),
            )
            for id_, kind, vec in items:
                await db.execute(
                    "INSERT INTO kb_vectors (id, datasource, kind, source_file, vector) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (id_, datasource, kind, source_file, _pack_vector(vec)),
                )
            await db.commit()

    async def delete_file(self, datasource: str, source_file: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_VECTORS)
            await db.execute(
                "DELETE FROM kb_vectors WHERE datasource = ? AND source_file = ?",
                (datasource, source_file),
            )
            await db.commit()

    async def clear(self, datasource: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_VECTORS)
            await db.execute(
                "DELETE FROM kb_vectors WHERE datasource = ?", (datasource,))
            await db.commit()

    async def query(
        self, datasource: str, vector: list[float], kinds: tuple[str, ...], limit: int,
    ) -> list[tuple[int, float]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(CREATE_VECTORS)
            ph = ",".join("?" * len(kinds))
            cursor = await db.execute(
                f"SELECT id, vector FROM kb_vectors "
                f"WHERE datasource = ? AND kind IN ({ph})",
                (datasource, *kinds),
            )
            rows = await cursor.fetchall()
        scored = [
            (r["id"], cosine(vector, _unpack_vector(r["vector"]))) for r in rows
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


class PgVectorStore:
    """Postgres + pgvector 后端(opt-in,独立 vector_dsn;依赖 uv sync --extra pgvector)。

    表结构:``kb_vectors(id BIGINT PRIMARY KEY, datasource TEXT, kind TEXT,
    source_file TEXT, vector vector, model TEXT)``。查询用 pgvector 的
    余弦距离算子 ``<=>``。
    """

    def __init__(self, dsn: str):
        self._dsn = dsn

    async def _connect(self):
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self._dsn)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_vectors ("
            "id BIGINT PRIMARY KEY, datasource TEXT NOT NULL, kind TEXT NOT NULL, "
            "source_file TEXT NOT NULL, vector vector NOT NULL, model TEXT)"
        )
        await conn.commit()
        return conn

    @staticmethod
    def _lit(vector: list[float]) -> str:
        return "[" + ",".join(f"{float(x):.8f}" for x in vector) + "]"

    async def replace(self, datasource: str, source_file: str,
                      items: list[tuple[int, str, list[float]]]) -> None:
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM kb_vectors WHERE datasource = %s AND source_file = %s",
                    (datasource, source_file),
                )
                for id_, kind, vec in items:
                    await cur.execute(
                        "INSERT INTO kb_vectors (id, datasource, kind, source_file, vector) "
                        "VALUES (%s, %s, %s, %s, %s::vector)",
                        (id_, datasource, kind, source_file, self._lit(vec)),
                    )
            await conn.commit()
        finally:
            await conn.close()

    async def delete_file(self, datasource: str, source_file: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                "DELETE FROM kb_vectors WHERE datasource = %s AND source_file = %s",
                (datasource, source_file),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def clear(self, datasource: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                "DELETE FROM kb_vectors WHERE datasource = %s", (datasource,))
            await conn.commit()
        finally:
            await conn.close()

    async def query(
        self, datasource: str, vector: list[float], kinds: tuple[str, ...], limit: int,
    ) -> list[tuple[int, float]]:
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, 1 - (vector <=> %s::vector) AS sim "
                    "FROM kb_vectors WHERE datasource = %s AND kind = ANY(%s) "
                    "ORDER BY vector <=> %s::vector LIMIT %s",
                    (self._lit(vector), datasource, list(kinds),
                     self._lit(vector), limit),
                )
                rows = await cur.fetchall()
            return [(int(r[0]), float(r[1])) for r in rows]
        finally:
            await conn.close()


def _vector_dsn(cfg: Any) -> str:
    """向量库连接串:显式 vector_dsn 优先;留空时从 postgres 业务库同实例推导。

    ``postgresql://user:pass@host:port/database`` 由 DatasourceConfig 的
    connection_params + credentials 拼出(与 postgres 业务适配器同源)。
    业务库非 postgres 且无显式 dsn → 空(调用方退化为 sqlite 本地向量)。
    """
    explicit = str(getattr(cfg, "vector_dsn", "") or "").strip()
    if explicit:
        return explicit
    if getattr(cfg, "type", "") != "postgres":
        return ""
    params = {
        **(getattr(cfg, "connection_params", None) or {}),
        **(getattr(cfg, "credentials", None) or {}),
    }
    host = str(params.get("host", "127.0.0.1"))
    port = params.get("port", 5432)
    user = str(params.get("user", ""))
    password = str(params.get("password", ""))
    database = str(params.get("database", ""))
    auth = ""
    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    elif user:
        auth = f"{quote(user, safe='')}@"
    elif password:
        auth = f":{quote(password, safe='')}@"
    return f"postgresql://{auth}{host}:{port}/{database}"


def vector_store_for(kb: Any, cfg: Any) -> VectorStore:
    """按数据源配置构造向量后端(pgvector 默认;非 postgres 业务库退化为 sqlite)。

    - vector_backend=pgvector:用 vector_dsn,未配则从 postgres 业务库同实例
      推导(默认部署 = 一个 Postgres,业务表 + kb_vectors 共存)。
    - 缺 dsn(业务库非 postgres)/ 驱动不可用 → 退化为 sqlite 本地向量(降级不报错)。
    """
    backend = str(getattr(cfg, "vector_backend", "") or "pgvector")
    if backend == "pgvector":
        dsn = _vector_dsn(cfg)
        if dsn:
            try:
                return PgVectorStore(dsn)
            except Exception as e:  # pragma: no cover - 驱动缺失
                logger.warning(
                    "pgvector unavailable (%s); falling back to sqlite vectors", e)
    return SqliteVectorStore(kb)
