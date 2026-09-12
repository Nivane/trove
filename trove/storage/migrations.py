"""内部存储的版本化迁移 —— 每个库记下"我升到哪一版了"。

替代此前散落各处的**每次打开都跑的探测**(``PRAGMA table_info`` / 试探性
``ALTER TABLE`` 再吞掉异常):探测的代价是每次启动一次 IO,收益只是"看起来
能用";而真正需要的是"这个库已经在 v3"这个事实被记下来。

三件事是这一层存在的理由:

- **只跑一次**。``trove_schema(store, version, updated_at)`` 记下每个 store
  的版本;没记录 = 0 = 空库。库里有多个 store(生产 PG 同库多表),各记各的。
- **库比代码新 → 拒绝**。旧代码开新库是最静默的一种分歧:``SELECT`` 少几列
  不报错(列名可选),``INSERT`` 少几列更不报错,只是数据在悄悄丢。这不是
  "迁移没做",是"反向迁移没做"——所以它必须是一条明确的拒绝。
- **半迁移比不迁移更糟**。每个迁移版本在自己的事务里(含显式 ``BEGIN``:
  SQLite 的隐式事务不覆盖 DDL),失败整体回滚、**版本号不前进**。版本号前进
  而数据没落地,下一次运行会以"我已经升过了"为前提,再也不会重试。

**迁移操作必须逐条幂等**。这不是洁癖,是收养路径的全部依赖:存量库没有版本
记录,于是它会从 0 开始重放全部迁移 —— 建表语句带 ``IF NOT EXISTS``、加列走
:class:`AddColumn`(探测后补),重放就成了空操作,而版本落到当前。也正因为
逐条幂等,失败的那一步重跑是安全的。

**配置驱动的那一列不是迁移**。可选的列(如稀疏检索通道的 ``sparse``)有无由
配置决定,不由历史决定 —— 版本号必须只反映历史,否则改一次配置就能把库
"变新",或者反过来让一条本该执行的迁移被跳过。这类列走 :func:`ensure_column`。

**能重算的东西不需要迁移**。KB 镜像的每一行都能从 YAML 重新算出来,所以它的
版本不符的处置是**重建**而不是逐版迁移(见 ``KbService._ensure_mirror_schema``);
需要精细迁移的是不可重算的东西:会话、用户事实、episodes、审计日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

SQLITE = "sqlite"
POSTGRES = "postgres"

#: 版本表。store 是主键:一个物理库里住着多个 store(生产 PG 同库多表)。
SCHEMA_TABLE = "trove_schema"

_CREATE_SCHEMA_TABLE = f"""CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
    store TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
)"""


class StorageSchemaTooNew(RuntimeError):
    """库比代码新 —— 拒绝按旧 schema 读写(会静默丢数据)。"""


class StorageMigrationFailed(RuntimeError):
    """一步迁移失败并已回滚;版本号停在失败前的那一版。"""


@dataclass(frozen=True)
class AddColumn:
    """加一列。两方言的类型与幂等写法不同,翻译归迁移器。

    SQLite 没有 ``ADD COLUMN IF NOT EXISTS``(PG 有),所以幂等靠**探测**:
    这一条探测只在迁移真正执行时发生 —— 版本记录到位之后不再发生。
    """

    table: str
    column: str
    sqlite_type: str
    pg_type: str


#: 一条迁移的 op:原样执行的 SQL,或 :class:`AddColumn` 这类意图级操作。
Op = str | AddColumn


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    ops: list[Op] = field(default_factory=list)


def add_column_sql(op: AddColumn, dialect: str) -> str:
    """意图 → 方言 SQL。PG 原生带 ``IF NOT EXISTS``,SQLite 由调用方先探测。"""
    if dialect == POSTGRES:
        return (f"ALTER TABLE {op.table} ADD COLUMN IF NOT EXISTS "
                f"{op.column} {op.pg_type}")
    return f"ALTER TABLE {op.table} ADD COLUMN {op.column} {op.sqlite_type}"


def _ph(dialect: str) -> str:
    """占位符:store 代码保持 ``?``,裸 PG 连接要 ``%s``。"""
    return "%s" if dialect == POSTGRES else "?"


async def _fetchone(target: Any, sql: str, params: tuple = ()) -> Any:
    cursor = await (target.execute(sql, params) if params else target.execute(sql))
    return await cursor.fetchone()


async def table_exists(target: Any, table: str, *, dialect: str) -> bool:
    if dialect == POSTGRES:
        # to_regclass 返回 NULL 而不是抛错(不存在的表是正常情况,不是异常)。
        # 但 SELECT 恒有一行 —— fetchone 拿到的是 (None,) 元组,判断必须落在
        # 值上:拿元组判 ``is not None`` 会永远为真,于是版本表不存在时也去
        # SELECT 它 → UndefinedTable(空库的首建路径)。
        row = await _fetchone(
            target, f"SELECT to_regclass({_ph(dialect)})", (table,))
        return row is not None and row[0] is not None
    return await _fetchone(
        target,
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ) is not None


async def columns_of(target: Any, table: str, *, dialect: str) -> list[str]:
    """表的列名。SQLite 走 ``PRAGMA``,PG 走 information_schema。

    ``table`` 可以是 schema 限定的(``trove_retrieval.documents``)—— 检索
    store 的表在独立 schema 里,不限定就会去 ``public`` 找一个不存在的表,
    于是每次"探测"都以为列缺失。
    """
    if dialect == POSTGRES:
        schema, _, name = table.rpartition(".")
        if schema:
            where, params = "table_schema = ? AND table_name = ?", (schema, name)
        else:
            where, params = "table_schema = current_schema() AND table_name = ?", (name,)
        rows = await (await target.execute(
            f"SELECT column_name FROM information_schema.columns WHERE {where}",
            params,
        )).fetchall()
        return [r[0] for r in rows]
    rows = await (await target.execute(f"PRAGMA table_info({table})")).fetchall()
    return [r[1] for r in rows]


async def ensure_version_table(target: Any, *, dialect: str) -> None:
    await target.execute(_CREATE_SCHEMA_TABLE)
    await target.commit()


async def read_version(target: Any, store: str, *, dialect: str) -> int:
    """这个 store 记下的版本;无记录(含表还不存在)= 0 = 空库。"""
    if not await table_exists(target, SCHEMA_TABLE, dialect=dialect):
        return 0
    row = await _fetchone(
        target,
        f"SELECT version FROM {SCHEMA_TABLE} WHERE store = {_ph(dialect)}",
        (store,),
    )
    return int(row[0]) if row is not None else 0


async def write_version(target: Any, store: str, version: int, *, dialect: str) -> None:
    """记版本。

    ``ON CONFLICT`` 直接写在语句里(而不是 SQLite 的 ``INSERT OR REPLACE``):
    两后端同一串 SQL,不必靠后端改写 —— 这是仓库已有的约定。
    """
    ph = _ph(dialect)
    await target.execute(
        f"INSERT INTO {SCHEMA_TABLE} (store, version, updated_at) "
        f"VALUES ({ph}, {ph}, {ph}) "
        f"ON CONFLICT (store) DO UPDATE SET version = excluded.version, "
        f"updated_at = excluded.updated_at",
        (store, version, _now_iso()),
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _is_backend(target: Any) -> bool:
    """是不是 :class:`StorageBackend`(而不是裸连接)。

    ``rollback_pending`` 是后端独有的:它有一套**操作作用域**(一次操作
    必须以 commit() 或 close() 收尾),裸 aiosqlite / psycopg 连接没有。
    """
    return hasattr(target, "rollback_pending")


async def _end_read(target: Any) -> None:
    """只读操作也要给后端的作用域收尾。

    ``StorageBackend.execute`` 会为当前 task 取走作用域,直到 ``commit()``
    或 ``close()`` 才释放。只读不收尾的话,作用域会一直挂在这个 task 上 ——
    下一个请求是**另一个 task**,它会阻塞满 60s 才报错。裸连接什么都不用做:
    它们的 ``close()`` 是"断开连接",在这里调是灾难。
    """
    if _is_backend(target):
        await target.close()


async def _rollback(target: Any) -> None:
    """回滚当前步骤。后端自带操作作用域时失败即已回滚,这里是空操作。"""
    try:
        if _is_backend(target):
            await target.rollback_pending()
        else:
            await target.rollback()
    except Exception as e:  # 回滚失败不应掩盖原始错误
        logger.warning("storage migration rollback failed: %s", e)


async def _apply_op(target: Any, op: Op, dialect: str) -> None:
    if isinstance(op, AddColumn):
        if op.column in await columns_of(target, op.table, dialect=dialect):
            return
        await target.execute(add_column_sql(op, dialect))
        return
    await target.execute(op)


async def apply_migrations(
    target: Any,
    store: str,
    migrations: list[Migration],
    *,
    dialect: str,
) -> int:
    """把 store 升到代码已知的最高版本,返回应用后的版本。

    ``target`` 只要有 ``execute(sql[, params])`` 与 ``commit()`` 即可 ——
    :class:`~trove.storage.backends.base.StorageBackend`、裸 aiosqlite 连接、
    裸 psycopg 连接都能直接传进来(KB 镜像与检索 store 有意不在
    ``StorageBackend`` 上,见 CLAUDE.md,但它们同样需要"我是哪一版")。

    Raises:
        StorageSchemaTooNew: 库版本高于代码已知的最高版本 —— 拒绝,不执行
            任何语句。
        StorageMigrationFailed: 某一步失败,该步已整体回滚,版本未前进。
    """
    ordered = sorted(migrations, key=lambda m: m.version)
    known = ordered[-1].version if ordered else 0
    current = await read_version(target, store, dialect=dialect)
    # 只读也要收尾:不收的话"什么都不用做"这条最常见路径会把后端的作用域
    # 锁挂在当前 task 上,下一个请求要等满 60s 才报错。
    await _end_read(target)

    if current > known:
        raise StorageSchemaTooNew(
            f"storage '{store}' is at schema v{current} but this build only "
            f"knows v{known} — the database was written by a newer Trove. "
            f"Upgrade Trove (or point it at a different database); refusing to "
            f"read/write it with an older schema, which would silently lose data."
        )

    pending = [m for m in ordered if m.version > current]
    if pending:
        await ensure_version_table(target, dialect=dialect)
    for migration in pending:
        await _run_step(target, store, migration, dialect)
        current = migration.version
        logger.info(
            "storage '%s' migrated to v%d (%s)",
            store, migration.version, migration.description,
        )
    return current


async def _run_step(
    target: Any, store: str, migration: Migration, dialect: str,
) -> None:
    """一步 = 一个事务:ops + 版本号,一起成或一起败。"""
    try:
        if dialect == SQLITE:
            # SQLite 的隐式事务只覆盖 DML —— 不显式 BEGIN,建表/加列会各自
            # 自动提交,失败时留下半成品且回滚不掉。
            await target.execute("BEGIN")
        for op in migration.ops:
            await _apply_op(target, op, dialect)
        await write_version(target, store, migration.version, dialect=dialect)
        await target.commit()
    except BaseException as e:
        await _rollback(target)
        if not isinstance(e, Exception):     # 取消/中断:回滚后原样上抛
            raise
        raise StorageMigrationFailed(
            f"migration of storage '{store}' to v{migration.version} "
            f"({migration.description}) failed and was rolled back: {e}"
        ) from e


async def ensure_column(
    target: Any, op: AddColumn, *, dialect: str,
) -> bool:
    """:class:`AddColumn` 的无版本形式 —— **配置驱动**的可选列走这里。

    返回是否真的加了列。它的有无由配置决定,不由历史决定,因此不该被记进
    ``trove_schema``(见模块文档)。**调用方负责 commit**:它是一次普通写入,
    不是一次迁移步骤。
    """
    if op.column in await columns_of(target, op.table, dialect=dialect):
        return False
    await target.execute(add_column_sql(op, dialect))
    return True


__all__ = [
    "POSTGRES",
    "SCHEMA_TABLE",
    "SQLITE",
    "AddColumn",
    "Migration",
    "Op",
    "StorageMigrationFailed",
    "StorageSchemaTooNew",
    "add_column_sql",
    "apply_migrations",
    "columns_of",
    "ensure_column",
    "ensure_version_table",
    "read_version",
    "table_exists",
    "write_version",
]
