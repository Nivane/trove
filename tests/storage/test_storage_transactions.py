"""存储后端的操作事务边界。

后端只持有**一条**连接(sqlite 依赖单连接存续,PG 也复用同一条),而 store 的
写法统一是::

    conn = await self._conn()
    try:
        await conn.execute(...)   # 多条
        await conn.commit()
    finally:
        await conn.close()

连接唯一 → 事务唯一:两个并发请求在同一事务里交错插语句,**谁先 commit 就把
对方的在途语句一起发布了**;更糟的是全仓库原无任何 rollback,一次操作中途失败
留下的半成品,会被之后任意一次无关的 commit 带出去 —— 部分写入变成持久数据。

本文件把这两条固化成断言:操作必须是**独占**的,且失败必须**收口**(回滚)。
"""

import asyncio

import pytest

from trove.storage.backends.sqlite import SqliteBackend

_DDL = "CREATE TABLE t (x TEXT NOT NULL);"


@pytest.fixture
async def backend(tmp_path):
    """文件后端:外部观察者需要独立连接才能看到"已提交"的真实状态。"""
    b = SqliteBackend(str(tmp_path / "state.sqlite"))
    await b.executescript(_DDL)
    yield b
    await b.dispose()


async def _committed_rows(path) -> list[str]:
    """用一个**独立连接**读:只有真正提交的数据才可见。"""
    other = SqliteBackend(str(path))
    try:
        cur = await other.execute("SELECT x FROM t ORDER BY x")
        return [r[0] for r in await cur.fetchall()]
    finally:
        await other.dispose()


async def _another_task_can_operate(backend, value: str) -> None:
    """**另起一个 task** 跑一次完整操作;作用域未释放则会阻塞并超时。

    必须换 task:同一 task 的嵌套调用按设计复用外层作用域,留在原 task 里跑
    会假绿。超时即断言失败(wait_for 抛 TimeoutError)。
    """

    async def op() -> None:
        await backend.execute(f"INSERT INTO t VALUES ('{value}')")
        await backend.commit()

    await asyncio.wait_for(asyncio.create_task(op()), timeout=1.0)


class TestOperationIsolation:
    async def test_failed_operation_partial_writes_never_published(self, backend, tmp_path):
        """失败操作的半成品不得被后续无关的 commit 带出去。

        操作 A 写第一行成功、第二行违反 NOT NULL 失败,且不做 rollback(现状
        全仓库无 rollback);随后无关的操作 B 成功并 commit。修复前 B 的
        commit 会把 A 的半成品一起发布 —— 数据库里出现一条谁都没打算写的数据。
        """
        # 操作 A:第一条成功,第二条失败
        await backend.execute("INSERT INTO t VALUES ('a-partial')")
        with pytest.raises(Exception):
            await backend.execute("INSERT INTO t VALUES (NULL)")

        # 操作 B:无关的完整操作
        await backend.execute("INSERT INTO t VALUES ('b-ok')")
        await backend.commit()

        assert await _committed_rows(tmp_path / "state.sqlite") == ["b-ok"]

    async def test_operation_is_exclusive(self, backend):
        """一个操作未结束前,另一个操作不得插进它的事务。

        这是上一条的机制面:并发请求交错的前提是"能交错";作用域独占后,
        第二个操作必须等第一个结束(committed/closed),而不是挤进同一事务。
        """
        await backend.execute("INSERT INTO t VALUES ('a-1')")  # 操作 A 在途

        async def other() -> None:
            await backend.execute("INSERT INTO t VALUES ('b-1')")
            await backend.commit()

        task = asyncio.create_task(other())
        await asyncio.sleep(0.05)
        assert not task.done(), "另一个操作挤进了 A 的未结束事务"

        await backend.commit()   # A 结束 → B 才被放行
        await backend.close()
        await asyncio.wait_for(task, timeout=2)

    async def test_close_without_commit_rolls_back(self, backend, tmp_path):
        """操作只 close 不 commit(半途放弃)→ 写入必须丢弃。"""
        await backend.execute("INSERT INTO t VALUES ('abandoned')")
        await backend.close()

        assert await _committed_rows(tmp_path / "state.sqlite") == []

    async def test_commit_releases_the_scope(self, tmp_path):
        """execute → commit(不 close)也必须结束作用域。

        query_log.record 就是这种写法(且它的 except 会吞掉异常):作用域若只在
        close 时释放,该路径失败后会永久占住锁,整个进程的存储全部卡死。
        """
        b = SqliteBackend(str(tmp_path / "q.sqlite"))
        try:
            await b.executescript(_DDL)
            await b.execute("INSERT INTO t VALUES ('logged')")
            await b.commit()
            # 未 close,作用域也必须已释放:另一个 task 能立刻照常操作
            await _another_task_can_operate(b, "after-commit")
        finally:
            await b.dispose()

    async def test_statement_error_releases_the_scope(self, tmp_path):
        """语句失败 → 立即收口,不把锁留给"永远不 close"的调用方。

        query_log.record 吞异常且不 close:失败若仍占着锁,此后所有存储操作
        全部阻塞。
        """
        b = SqliteBackend(str(tmp_path / "q2.sqlite"))
        try:
            await b.executescript(_DDL)
            with pytest.raises(Exception):
                await b.execute("INSERT INTO t VALUES (NULL)")
            await _another_task_can_operate(b, "after-error")
        finally:
            await b.dispose()

    async def test_nested_helper_shares_the_outer_operation(self, backend, tmp_path):
        """同一 task 内的嵌套调用共用外层作用域(store 的 _upsert_meta 模式)。

        嵌套若各自取锁 → 自死锁;若各自 commit → 把一个操作劈成两半。
        """
        async def inner() -> None:
            await backend.execute("INSERT INTO t VALUES ('inner')")

        await backend.execute("INSERT INTO t VALUES ('outer')")
        await inner()                      # 同 task 嵌套,复用外层
        await backend.commit()
        await backend.close()

        assert await _committed_rows(tmp_path / "state.sqlite") == ["inner", "outer"]

    async def test_operation_is_atomic_across_two_readers(self, backend, tmp_path):
        """多语句操作对外是原子的:提交前外部连接看不到任何一行。"""
        await backend.execute("INSERT INTO t VALUES ('x1')")
        await backend.execute("INSERT INTO t VALUES ('x2')")
        assert await _committed_rows(tmp_path / "state.sqlite") == []

        await backend.commit()
        assert await _committed_rows(tmp_path / "state.sqlite") == ["x1", "x2"]


# ── Postgres:生产默认后端,此处用假连接覆盖其**独有**分支 ──────────
# 作用域算法本身在 base.py(已被上面的 SQLite 用例覆盖),这里只测 PG 特有的
# 两处:rollback_pending 的 IDLE 判断,以及语句失败后的收口(aborted 事务不
# 收拾干净会让后续所有请求以 InFailedSqlTransaction 失败)。

psycopg = pytest.importorskip("psycopg")


class _FakeInfo:
    def __init__(self) -> None:
        from psycopg.pq import TransactionStatus
        self.transaction_status = TransactionStatus.IDLE


class _FakePgConn:
    def __init__(self) -> None:
        self.closed = False
        self.info = _FakeInfo()
        self.commits = 0
        self.rollbacks = 0
        self.fail_next = False

    async def commit(self) -> None:
        self.commits += 1
        self._idle()

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._idle()

    async def execute(self, sql, params=()):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("syntax error")
        self._intrans()

    def _intrans(self) -> None:
        from psycopg.pq import TransactionStatus
        self.info.transaction_status = TransactionStatus.INTRANS

    def _idle(self) -> None:
        from psycopg.pq import TransactionStatus
        self.info.transaction_status = TransactionStatus.IDLE

    def cursor(self):
        return _FakePgCursor(self)


class _FakePgCursor:
    def __init__(self, conn: _FakePgConn) -> None:
        self._conn = conn

    async def execute(self, sql, params=()) -> None:
        if self._conn.fail_next:
            self._conn.fail_next = False
            self._conn._intrans()   # PG:错误语句会把事务打成 aborted
            raise RuntimeError("syntax error")
        self._conn._intrans()

    async def fetchone(self):
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def pg_backend():
    from trove.storage.backends.postgres import PostgresBackend
    from psycopg.pq import TransactionStatus

    b = PostgresBackend("postgresql://unused/unused")
    b._conn = _FakePgConn()
    yield b, b._conn, TransactionStatus


class TestPostgresScope:
    async def test_rollback_pending_is_free_when_idle(self, pg_backend):
        """事务空闲 → 不发 ROLLBACK(纯读操作不因此多一次往返)。"""
        b, conn, _ = pg_backend
        await b.rollback_pending()
        assert conn.rollbacks == 0

    async def test_rollback_pending_rolls_back_open_transaction(self, pg_backend):
        b, conn, _ = pg_backend
        conn._intrans()
        await b.rollback_pending()
        assert conn.rollbacks == 1

    async def test_statement_error_rolls_back_and_releases_scope(self, pg_backend):
        """语句失败 → 立即回滚 + 释放作用域。

        PG 的错误语句把事务打成 aborted;共享连接上若无人回滚,下一个请求的
        每条语句都会以 InFailedSqlTransaction 失败。
        """
        b, conn, _ = pg_backend
        conn.fail_next = True
        with pytest.raises(RuntimeError):
            await b.execute("INSERT INTO t VALUES ('x')")
        assert conn.rollbacks == 1
        # 作用域已释放:另一个 task 不会被卡住
        async def other() -> None:
            await b.execute("INSERT INTO t VALUES ('y')")
            await b.commit()

        await asyncio.wait_for(asyncio.create_task(other()), timeout=1.0)

    async def test_close_rolls_back_and_releases(self, pg_backend):
        b, conn, _ = pg_backend
        await b.execute("INSERT INTO t VALUES ('x')")
        await b.close()
        assert conn.rollbacks == 1
        assert conn.commits == 0

    async def test_commit_ends_scope(self, pg_backend):
        b, conn, _ = pg_backend
        await b.execute("INSERT INTO t VALUES ('x')")
        await b.commit()
        assert conn.commits == 1
        assert conn.rollbacks == 0
        # 已结束 → close 不该再回滚
        await b.close()
        assert conn.rollbacks == 0
