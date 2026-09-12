"""内部存储的版本化迁移 —— C4(design §3.5,验收 §5 第 8、9 条)。

这一层要证明的三件事:

1. **迁移只跑一次**。"每次打开都探测(PRAGMA / 试 ALTER)"换成"这个库已经在
   哪一版"被记下来 —— 记下来的东西不需要反复问。
2. **库比代码新 → 拒绝**。旧代码开新库是最静默的一种分歧:SELECT 少列不报错,
   INSERT 少列也不报错,只是数据在丢。
3. **半迁移比不迁移更糟**。步骤失败 → 事务回滚 + 版本号不前进。
"""

from __future__ import annotations

import aiosqlite
import pytest

from trove.storage.migrations import (
    POSTGRES,
    SQLITE,
    AddColumn,
    Migration,
    StorageMigrationFailed,
    StorageSchemaTooNew,
    add_column_sql,
    apply_migrations,
    columns_of,
    ensure_column,
    ensure_version_table,
    read_version,
    table_exists,
    write_version,
)


@pytest.fixture
async def conn(tmp_path):
    async with aiosqlite.connect(tmp_path / "store.sqlite") as db:
        yield db


def _v1() -> Migration:
    """一版"建表"迁移。**故意不加 IF NOT EXISTS** —— 重跑就会炸,于是
    "第二次运行是空操作"这条不变量可以用行为证明,不需要计数器。"""
    return Migration(version=1, description="建 probe 表",
                     ops=["CREATE TABLE probe (n INTEGER)"])


class TestApplying:
    async def test_fresh_database_runs_every_migration(self, conn):
        applied = await apply_migrations(conn, "demo", [
            _v1(),
            Migration(2, "加一列", ops=[AddColumn("probe", "note", "TEXT", "TEXT")]),
        ], dialect=SQLITE)

        assert applied == 2
        assert await read_version(conn, "demo", dialect=SQLITE) == 2
        assert "note" in await columns_of(conn, "probe", dialect=SQLITE)

    async def test_applied_migration_is_not_rerun(self, conn):
        migrations = [_v1()]
        await apply_migrations(conn, "demo", migrations, dialect=SQLITE)
        # v1 的 CREATE 没有 IF NOT EXISTS:真重跑会抛 "table probe already exists"
        assert await apply_migrations(conn, "demo", migrations, dialect=SQLITE) == 1

    async def test_version_is_per_store(self, conn):
        """一个库里住着多个 store(生产 PG 就是这样),版本各记各的。"""
        await apply_migrations(conn, "a", [_v1()], dialect=SQLITE)
        assert await read_version(conn, "a", dialect=SQLITE) == 1
        assert await read_version(conn, "b", dialect=SQLITE) == 0

    async def test_a_new_migration_applies_on_top(self, conn):
        await apply_migrations(conn, "demo", [_v1()], dialect=SQLITE)
        applied = await apply_migrations(conn, "demo", [
            _v1(),
            Migration(2, "加一列", ops=[AddColumn("probe", "note", "TEXT", "TEXT")]),
        ], dialect=SQLITE)
        assert applied == 2
        assert "note" in await columns_of(conn, "probe", dialect=SQLITE)

    async def test_add_column_tolerates_a_column_that_is_already_there(self, conn):
        """收养存量库:表是老代码建的(列已经有了),版本记录却是空的。

        没有 `ADD COLUMN IF NOT EXISTS` 的 SQLite 上,这一步会直接抛
        "duplicate column name" —— 探测吸收了它,于是**新库/存量库/重跑**
        走的是同一条路径(这也是"迁移必须逐条幂等"这条不变量的一半)。
        """
        await conn.execute("CREATE TABLE probe (n INTEGER, note TEXT)")
        await conn.commit()

        await apply_migrations(conn, "demo", [
            # 真实的 v1 是 `IF NOT EXISTS` 的建表 —— 收养一个已有的库时它是空操作
            Migration(1, "建 probe 表",
                      ops=["CREATE TABLE IF NOT EXISTS probe (n INTEGER)"]),
            Migration(2, "加一列", ops=[AddColumn("probe", "note", "TEXT", "TEXT")]),
        ], dialect=SQLITE)
        assert await read_version(conn, "demo", dialect=SQLITE) == 2
        assert (await columns_of(conn, "probe", dialect=SQLITE)).count("note") == 1


class TestDialectTranslation:
    """意图级操作 → 各方言 SQL。写在迁移里的是一件事,不是两串 SQL。"""

    op = AddColumn("episodes", "embedding", "BLOB", "BYTEA")

    def test_sqlite_has_no_if_not_exists(self):
        """SQLite 没有 `ADD COLUMN IF NOT EXISTS` —— 幂等由探测保证。"""
        assert add_column_sql(self.op, SQLITE) == (
            "ALTER TABLE episodes ADD COLUMN embedding BLOB"
        )

    def test_postgres_uses_if_not_exists(self):
        assert add_column_sql(self.op, POSTGRES) == (
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS embedding BYTEA"
        )


class TestTableExistsPostgres:
    """PG 方言的 ``to_regclass`` 探测:不存在的表返回的是一行 ``(NULL,)``。

    判断必须落在**值**上而不是行元组上 —— 拿 ``(None,) is not None`` 判永远
    为真,空库首建时 ``read_version`` 就会去 SELECT 不存在的 ``trove_schema``
    → UndefinedTable(CI 真实 PG 上抓到的回归,SQLite 侧测不到:它的探测在
    无行时返回 None,恰好判对了)。
    """

    class _Cursor:
        def __init__(self, value):
            self._value = value

        async def fetchone(self):
            return self._value

    class _FakeTarget:
        def __init__(self, existing: bool):
            self._existing = existing

        async def execute(self, sql, params=()):
            # to_regclass:存在的表返回 oid,不存在的表返回 NULL(仍是一行)
            return TestTableExistsPostgres._Cursor(
                (16384,) if self._existing else (None,))

    async def test_missing_table_is_not_seen_as_existing(self):
        assert await table_exists(
            self._FakeTarget(existing=False), "trove_schema",
            dialect=POSTGRES) is False

    async def test_existing_table_is_detected(self):
        assert await table_exists(
            self._FakeTarget(existing=True), "trove_schema",
            dialect=POSTGRES) is True


class TestDirection:
    async def test_database_newer_than_code_is_refused(self, conn):
        """验收 #8:库版本 > 代码已知 → 拒绝,并且什么都不执行。"""
        await ensure_version_table(conn, dialect=SQLITE)
        await write_version(conn, "demo", 99, dialect=SQLITE)

        with pytest.raises(StorageSchemaTooNew) as excinfo:
            await apply_migrations(conn, "demo", [_v1()], dialect=SQLITE)

        assert "99" in str(excinfo.value)
        assert not await table_exists(conn, "probe", dialect=SQLITE), (
            "拒绝必须是拒绝:不能先执行一半再报错"
        )
        assert await read_version(conn, "demo", dialect=SQLITE) == 99, (
            "版本号不能被旧代码改回去"
        )


class TestFailure:
    async def test_failed_step_rolls_back_and_does_not_advance(self, conn):
        """验收 #9:中途失败 → 数据回滚 + 版本不前进。

        版本号前进而数据没落地,比停在原地糟得多 —— 下一次运行会以"我已经
        升到 v2 了"为前提,再也不会重试。
        """
        await apply_migrations(conn, "demo", [_v1()], dialect=SQLITE)

        broken = Migration(2, "写坏的迁移", ops=[
            "CREATE TABLE half (x INTEGER)",
            "THIS IS NOT SQL",
        ])
        with pytest.raises(StorageMigrationFailed):
            await apply_migrations(conn, "demo", [_v1(), broken], dialect=SQLITE)

        assert await read_version(conn, "demo", dialect=SQLITE) == 1
        assert not await table_exists(conn, "half", dialect=SQLITE), (
            "同一步里的半成品必须一起回滚(SQLite 的 DDL 默认不进隐式事务,"
            "靠迁移器显式 BEGIN)"
        )

    async def test_the_failed_step_is_retried_after_a_fix(self, conn):
        """版本没前进 → 修好之后重跑仍然会执行它(而不是被跳过)。"""
        await apply_migrations(conn, "demo", [_v1()], dialect=SQLITE)
        fixed = Migration(2, "修好的迁移", ops=[
            "CREATE TABLE half (x INTEGER)",
            AddColumn("probe", "note", "TEXT", "TEXT"),
        ])
        assert await apply_migrations(conn, "demo", [_v1(), fixed],
                                      dialect=SQLITE) == 2
        assert "note" in await columns_of(conn, "probe", dialect=SQLITE)


class TestOperationScope:
    """迁移器用的后端是**共享连接 + 操作作用域**:一次操作必须以 commit()
    或 close() 收尾,否则作用域锁留在当前 task 上,下一个请求(另一个 task)
    要阻塞满 60s 才报错。只读路径最容易漏 —— 它看起来什么都不用提交。"""

    async def test_a_no_op_run_does_not_leave_the_scope_held(self, tmp_path):
        import asyncio

        from trove.services.memory.episode import EpisodeStore

        store = EpisodeStore(tmp_path / "episodes.sqlite")
        await store._ensure_schema()          # 建库:有活干,commit 收尾

        # 第二次是"什么都不用做"—— 最常见的路径
        fresh = EpisodeStore(tmp_path / "episodes.sqlite")
        await fresh._ensure_schema()

        async def another_request():
            return await fresh._backend.execute("SELECT 1")

        await asyncio.wait_for(asyncio.create_task(another_request()), timeout=1)


class TestEnsureColumn:
    """配置驱动的那一列**不是迁移**:它的有无由配置决定,不由历史决定。

    版本号必须只反映历史 —— 否则改一次配置就能把库"变新"(或者反过来,
    让一条本该执行的迁移被跳过)。
    """

    async def test_adds_once_then_reports_present(self, conn):
        await conn.execute("CREATE TABLE probe (n INTEGER)")
        await conn.commit()
        op = AddColumn("probe", "note", "TEXT", "TEXT")

        assert await ensure_column(conn, op, dialect=SQLITE) is True
        assert await ensure_column(conn, op, dialect=SQLITE) is False
        assert (await columns_of(conn, "probe", dialect=SQLITE)).count("note") == 1


class TestAbsorbedSites:
    """四处手写迁移的收编:记忆的 embedding 列。"""

    @staticmethod
    async def _legacy_episodes(path):
        """老代码建的 episodes 库:有表、有数据、**没有** embedding 列。"""
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id TEXT NOT NULL, datasource TEXT NOT NULL, "
                "session_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '', "
                "question TEXT NOT NULL, sql TEXT NOT NULL DEFAULT '', "
                "dialect TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL DEFAULT '', "
                "row_count INTEGER NOT NULL DEFAULT -1, "
                "result_signature TEXT NOT NULL DEFAULT '', "
                "correction_history TEXT NOT NULL DEFAULT '[]', "
                "matched_tables TEXT NOT NULL DEFAULT '[]', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            await db.commit()

    async def test_legacy_episodes_db_gets_the_embedding_column(self, tmp_path):
        from trove.services.memory.episode import EpisodeStore
        from trove.services.memory.models import MemoryScope

        db_path = tmp_path / "episodes.sqlite"
        await self._legacy_episodes(db_path)

        store = EpisodeStore(db_path)
        await store.record(
            MemoryScope(datasource="demo", user_id="u1"),
            question="贷款总额", sql="SELECT 1",
        )

        async with aiosqlite.connect(db_path) as db:
            assert "embedding" in await columns_of(db, "episodes", dialect=SQLITE)
            assert await read_version(db, "memory", dialect=SQLITE) == 2

    async def test_adopted_store_is_not_probed_again(self, tmp_path):
        """收养之后**不再探测**:列被人删掉也不会被"补"回来 —— 版本记录
        说它已经是这一版了。今天的实现每次打开都试一次 ALTER,做不到这条。"""
        from trove.services.memory.episode import EpisodeStore

        db_path = tmp_path / "episodes.sqlite"
        await self._legacy_episodes(db_path)
        store = EpisodeStore(db_path)
        await store._ensure_schema()   # 收养:加列 + 记版本

        async with aiosqlite.connect(db_path) as db:
            await db.execute("ALTER TABLE episodes DROP COLUMN embedding")
            await db.commit()

        fresh = EpisodeStore(db_path)
        await fresh._ensure_schema()
        async with aiosqlite.connect(db_path) as db:
            assert "embedding" not in await columns_of(db, "episodes", dialect=SQLITE)

    @staticmethod
    async def _legacy_retrieval_db(path):
        """老代码建的检索库:documents 有 embedding 但**没有** sparse 列。"""
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "CREATE TABLE documents (rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
                "doc_id TEXT UNIQUE NOT NULL, datasource TEXT NOT NULL, "
                "kind TEXT NOT NULL, source_file TEXT NOT NULL DEFAULT '', "
                "content TEXT NOT NULL, embedding BLOB)")
            await db.execute(
                "CREATE VIRTUAL TABLE doc_fts USING fts5(content, content_rowid)")
            await db.execute(
                "CREATE INDEX idx_documents_ds ON documents(datasource)")
            await db.commit()

    async def test_legacy_retrieval_db_gets_the_sparse_column(self, tmp_path):
        from trove.services.retrieval.sqlite_store import SqliteHybridStore

        db_path = tmp_path / "retrieval.sqlite"
        await self._legacy_retrieval_db(db_path)

        await SqliteHybridStore(db_path, None, None)._ensure()

        async with aiosqlite.connect(db_path) as db:
            assert "sparse" in await columns_of(db, "documents", dialect=SQLITE)
            assert await read_version(db, "retrieval", dialect=SQLITE) == 2

    async def test_fresh_retrieval_db_lands_on_the_same_version(self, tmp_path):
        """新库与存量库**最终同形** —— 这正是"迁移逐条幂等"换来的性质。"""
        from trove.services.retrieval.sqlite_store import SqliteHybridStore

        db_path = tmp_path / "retrieval.sqlite"
        await SqliteHybridStore(db_path, None, None)._ensure()

        async with aiosqlite.connect(db_path) as db:
            assert "sparse" in await columns_of(db, "documents", dialect=SQLITE)
            assert await read_version(db, "retrieval", dialect=SQLITE) == 2
