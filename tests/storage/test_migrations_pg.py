"""版本化迁移在真实 PostgreSQL 上 —— 方言翻译与事务语义的端到端核对。

Env-gated integration test(未设 PG_TEST_URL 自动跳过):

    PG_TEST_URL=postgresql://trove:trove@localhost:5432/trove \
        uv run pytest -m integration tests/storage/test_migrations_pg.py

本机没有 PG 时这一组**跑不到**,所以 SQLite 侧的用例必须把机制本身钉死
(tests/storage/test_migrations.py);这里补的是只在 PG 上才会露头的两类:
``ADD COLUMN IF NOT EXISTS`` 走不走得通,以及失败一步之后 PG 的
"事务已 aborted"状态有没有被收拾干净。
"""

from __future__ import annotations

import os
import uuid

import pytest

from trove.storage.backends import PostgresBackend
from trove.storage.migrations import (
    POSTGRES,
    AddColumn,
    Migration,
    StorageMigrationFailed,
    StorageSchemaTooNew,
    apply_migrations,
    read_version,
)

PG_URL = os.environ.get("PG_TEST_URL")

pytestmark = pytest.mark.integration


@pytest.fixture
async def backend():
    if not PG_URL:
        pytest.skip("PG_TEST_URL not set")
    # 每个用例一个独立 schema:同一个 PG 实例上并行跑也不互相踩。
    be = PostgresBackend(PG_URL, schema=f"mig_{uuid.uuid4().hex[:12]}")
    try:
        yield be
    finally:
        await be.dispose()


def _migrations() -> list[Migration]:
    return [
        Migration(1, "probe 表", ops=[
            "CREATE TABLE probe (n INTEGER)",
            "CREATE INDEX idx_probe_n ON probe(n)",
        ]),
        Migration(2, "note 列", ops=[AddColumn("probe", "note", "TEXT", "TEXT")]),
    ]


class TestOnPostgres:
    async def test_fresh_database_reaches_the_top_version(self, backend):
        assert await apply_migrations(backend, "demo", _migrations(),
                                      dialect=POSTGRES) == 2
        assert await read_version(backend, "demo", dialect=POSTGRES) == 2

    async def test_rerun_is_a_no_op(self, backend):
        await apply_migrations(backend, "demo", _migrations(), dialect=POSTGRES)
        # v1 的 CREATE 没有 IF NOT EXISTS:重跑会抛 duplicate_table
        assert await apply_migrations(backend, "demo", _migrations(),
                                      dialect=POSTGRES) == 2

    async def test_add_column_tolerates_an_existing_column(self, backend):
        """PG 有 `ADD COLUMN IF NOT EXISTS`;探测与它互为印证。"""
        await apply_migrations(backend, "demo", [_migrations()[0]],
                               dialect=POSTGRES)
        await backend.execute("ALTER TABLE probe ADD COLUMN note TEXT")
        await backend.commit()

        assert await apply_migrations(backend, "demo", _migrations(),
                                      dialect=POSTGRES) == 2

    async def test_too_new_database_is_refused(self, backend):
        await apply_migrations(backend, "demo", _migrations(), dialect=POSTGRES)
        too_new = _migrations() + [
            Migration(99, "未来的迁移", ops=["CREATE TABLE future (x INTEGER)"]),
        ]
        await apply_migrations(backend, "demo", too_new, dialect=POSTGRES)

        with pytest.raises(StorageSchemaTooNew):
            await apply_migrations(backend, "demo", _migrations(), dialect=POSTGRES)

    async def test_failed_step_rolls_back_and_the_connection_stays_usable(
        self, backend,
    ):
        """PG 的语句错误会把整个事务置为 aborted —— 回滚必须发生,否则之后
        每一句都以 InFailedSqlTransaction 失败,而共享连接上"之后"就是
        另一个无关的请求。"""
        await apply_migrations(backend, "demo", [_migrations()[0]],
                               dialect=POSTGRES)
        broken = Migration(2, "写坏的迁移", ops=[
            "ALTER TABLE probe ADD COLUMN half TEXT",
            "THIS IS NOT SQL",
        ])
        with pytest.raises(StorageMigrationFailed):
            await apply_migrations(backend, "demo", [_migrations()[0], broken],
                                   dialect=POSTGRES)

        assert await read_version(backend, "demo", dialect=POSTGRES) == 1
        # 连接还能用:半成品已回滚,事务不在 aborted 状态
        cursor = await backend.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'probe' AND table_schema = current_schema()")
        assert "half" not in [r[0] for r in await cursor.fetchall()]
        await backend.commit()
