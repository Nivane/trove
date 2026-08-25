"""Retrieval backend protocol — per-datasource switchable retrieval.

每个数据源可选择检索后端(读时 dispatch):builtin(确定性 + hashed
n-gram)或 hybrid(FTS5 + BM25 稀疏通道,后续加入 dense 通道)。三个
search 方法与 KbService 的公开方法同签名,返回类型与 builtin 一致
(term → TermHit、example → ExampleHit、lesson → dict)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trove.services.kb.service import ExampleHit, TermHit


@runtime_checkable
class RetrievalBackend(Protocol):
    """一个数据源检索后端的检索接口(terms / examples / lessons)。"""

    async def search_terms(
        self,
        question: str,
        datasource: str,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[Any]:  # list[TermHit]
        ...

    async def search_examples(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
        per_table: bool = False,
    ) -> list[Any]:  # list[ExampleHit]
        ...

    async def search_lessons(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[dict]:
        ...


@runtime_checkable
class SearchIndexBackend(RetrievalBackend, Protocol):
    """检索 + 索引同步后端(rag):镜像同步后重建/删除该数据源的向量。

    KbService 在 kb_items/kb_fts 同步完成后按需调用(duck-type 检测,
    缺失 = 无向量索引,如 builtin/hybrid)。
    """

    async def index_file(
        self, datasource: str, source_file: str,
        entries: list[tuple[str, str, dict]],
    ) -> None:
        """重建一个文件的向量索引(YAML 条目 → embedding → upsert)。"""

    async def delete_file(self, datasource: str, source_file: str) -> None:
        """删除一个文件的向量(删除传播)。"""

    async def clear(self, datasource: str) -> None:
        """清空一个数据源的全部向量(delete_kb)。"""
