"""资产使用台账 —— 每条资产被用了几次、成没成、多慢(设计稿 §6.2 / §6.3)。

**为什么统计不写进 ``examples.yml``**:那个目录被 ``git_versioning`` 版本化,
每次 KB 写入都 commit 一次。把高频运行时数据写进去,资产库的 git 历史会被统计
噪声淹没,真正想回看的那次「某条 SQL 被谁改坏了」也就找不到了(设计 §8.1)。

**为什么治理字段反而留在 YAML**:判据是「要不要跟着 SQL 一起回滚」。``runs`` /
``p50_ms`` 是随时间累积的观测 —— 回滚一条坏 SQL 不该把它们抹掉;``approved_by`` /
``status`` 是资产的属性,必须和 SQL 同生共死:否则回滚了坏 SQL,它的「已认证」
标记还留在库里(设计 §8.1 C)。

**I2 是本模块唯一贯穿全文件的纪律**:落账失败只记日志,绝不让查询失败。三个
公开方法的异常出口都收在这里。代价是失败对调用方**不可见** —— 所以每次吞掉都
要计入 ``trove_asset_ledger_failures_total``:调用方看不见的失败,正是指标存在
的理由。

**I7**:统计是纯 DB 写入,不注入 LLM 也能跑通 —— 本模块不 import 任何 LLM 依赖
(有测试钉住,含传递依赖)。
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from trove.core import metrics
from trove.core.logging import get_logger

logger = get_logger(__name__)

#: 一次使用的结局。**闭集** —— 放进来一个不认识的,``runs`` 就等于三个分桶
#: 之和加一个没人能解释的余数,那条断言(有测试)当场作废。
VERDICT_OK = "OK"
VERDICT_EMPTY = "EMPTY"
VERDICT_ERROR = "ERROR"
VERDICTS = frozenset({VERDICT_OK, VERDICT_EMPTY, VERDICT_ERROR})

#: 这次使用走的路径。fast_path = 快径命中模板直用;retrieval = 检索后编译/生成。
PATH_FAST = "fast_path"
PATH_RETRIEVAL = "retrieval"
PATHS = frozenset({PATH_FAST, PATH_RETRIEVAL})

#: 在 ``trove_schema`` 里记版本时用的 store 名。
STORE_NAME = "asset_ledger"

LEDGER_DIR_NAME = "assets"
LEDGER_FILE_NAME = "asset_ledger.sqlite"

USAGE_TABLE = "asset_usage"
EVENTS_TABLE = "asset_usage_events"

#: 明细保留期(天)。明细是分位数的唯一来源,但只增不减的表最后没人敢动 ——
#: 保留期就是它的出口。90 天足够覆盖"最近慢不慢",也不会把库撑大。
DEFAULT_EVENTS_RETENTION_DAYS = 90


def normalize_question(question: str) -> str:
    """问句归一化:小写 + 非单词字符置空格 + 折叠空白(``\\W`` 保留 CJK 等
    Unicode 字母)。

    与 ``SessionManager._normalize_question`` **同一套算法**(有测试钉住不许
    漂移):那边拿它做结果缓存的键,这边拿它做资产主键 —— 漂移了,同一句问句
    在缓存和台账里会是两个身份。

    与 ``trove/eval/baseline.py`` 的 ``_norm`` **有意不同**:那个是评测对账键,
    额外把 ``_`` 也折成空格。这里折了会把「问某个列名」和「问一句自然语言」
    并成一条资产 —— 少记一条只是多一行,合并错一条是两条资产的统计串台。
    """
    text = (question or "").lower()
    text = re.sub(r"\W", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def asset_key(question: str) -> str:
    """资产主键 = ``sha256(normalize(question))[:16]``(设计 §6.3)。

    **为什么不用 question 原文**:确认流程会重写问题措辞(``_carryover`` 场景),
    用原文当主键等于每改写一次就把这条资产的历史清零重来 —— 而改写恰恰是资产
    **变得更好**的时候,却把它的 ``runs`` 抹了。

    16 个十六进制字符 = 64 位:碰撞概率 2⁻⁶⁴,对一个几千条资产的台账可以忽略;
    换来的 SQL 主键短一半,``asset_usage_events`` 的索引也小一半。
    """
    digest = hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class AssetUsage:
    """一条资产的累计使用情况(设计 §6.2 的 ``asset_usage`` 行)。

    ``p50_ms`` 与两个时间戳都可空,且**空不是 0**:``p50_ms is None`` 表示一次
    都没测到耗时(无从谈快慢),``last_used_at is None`` 表示**根本没有行** ——
    只有落过账的资产才会有一行,所以「从未用过」在 ``usage()`` 里表现为**键
    缺席**,而不是一个 runs=0 的对象(设计 §8.2 的冷启动判断要的正是这个区别)。
    """

    runs: int = 0
    success_runs: int = 0
    empty_runs: int = 0
    error_runs: int = 0
    p50_ms: int | None = None
    last_used_at: str | None = None
    first_used_at: str | None = None


def _closed(value: Any, allowed: frozenset[str], field: str) -> str:
    """把枚举值收进闭集,返回**集合里那个规范拼法**。只对形式(大小写/空白)
    宽容,不对身份宽容。

    按小写查表而不是统一 ``.upper()``:两个集合的规范拼法不同(verdict 是大写
    ``OK``,path 是小写 ``fast_path``),统一折向任何一边都会把另一边的值全部
    判成非法 —— 而那种 bug 的表现是"整条链路一条都没记上",静默且致命。

    不做别名(不认 ``SUCCESS`` = ``OK``):两种拼法都能进,迟早有人写第三种,
    而统计口径一旦有第二种写法,分桶就不再是分桶了。
    """
    canonical = {v.lower(): v for v in allowed}
    key = str(value or "").strip().lower()
    if key not in canonical:
        raise ValueError(
            f"unknown {field} {value!r}; expected one of {sorted(allowed)}"
        )
    return canonical[key]


def _usage_ddl() -> list[str]:
    """两张表 + 索引(设计 §6.2)。

    ``UNIQUE (datasource, asset_key)`` 是聚合行能被 upsert 的前提,也是
    「同一问句在不同库上是不同资产」的实现(设计 §3.2 N3:不做跨源共享)。

    ``asset_usage_events`` 是**唯一真相来源**,``asset_usage`` 是它的物化视图 ——
    分位数只能从明细算:聚合行里存不下分布,而平均数会把一条 30 秒的慢查询
    (和三条快查询一起)藏得看不出来。
    """
    return [
        f"""
CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource     TEXT NOT NULL,
    asset_key      TEXT NOT NULL,
    runs           INTEGER NOT NULL DEFAULT 0,
    success_runs   INTEGER NOT NULL DEFAULT 0,
    empty_runs     INTEGER NOT NULL DEFAULT 0,
    error_runs     INTEGER NOT NULL DEFAULT 0,
    p50_ms         INTEGER,
    last_used_at   TEXT,
    first_used_at  TEXT,
    UNIQUE (datasource, asset_key)
)
""",
        f"""
CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource   TEXT NOT NULL,
    asset_key    TEXT NOT NULL,
    used_at      TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    elapsed_ms   INTEGER,
    path         TEXT NOT NULL
)
""",
        f"CREATE INDEX IF NOT EXISTS idx_asset_events "
        f"ON {EVENTS_TABLE} (datasource, asset_key, used_at)",
        f"CREATE INDEX IF NOT EXISTS idx_asset_events_age "
        f"ON {EVENTS_TABLE} (used_at)",
    ]


def _migrations():
    from trove.storage.migrations import Migration

    return [
        Migration(
            version=1,
            description="asset_usage(物化聚合)+ asset_usage_events(明细)与索引",
            ops=_usage_ddl(),
        ),
    ]


class AssetLedger:
    """资产台账。走现成的 :class:`StorageBackend`(SQLite 本地 / PG 生产),
    **不自建连接管理** —— 与 ``QueryLogRecorder`` / ``DriftStore`` 同一套。

    表结构走 ``storage/migrations`` 而非"每次打开都探测":版本要反映**历史**,
    不是反映每次启动时「看起来能用」;库比代码新时必须拒绝而不是按旧 schema
    静默丢列(见 ``storage/migrations.py`` 的模块 docstring)。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        events_retention_days: int = DEFAULT_EVENTS_RETENTION_DAYS,
    ) -> None:
        self.db_path = str(path)
        self.enabled = bool(enabled)
        self.events_retention_days = max(0, int(events_retention_days))
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(self.db_path)
        self._schema_ready = False

    @classmethod
    def for_home(cls, home: str | Path, config: Any = None) -> "AssetLedger":
        """``<home>/assets/asset_ledger.sqlite``。

        放在 ``<home>/assets/`` 而**不是** ``<home>/kb/``:kb 目录是被
        ``git_versioning`` 版本化的资产目录,把高频统计库放进去,等于把一块
        永远在变的二进制塞进版本控制(设计 §6.2 选择独立存储的同一个理由)。
        """
        enabled, retention = True, DEFAULT_EVENTS_RETENTION_DAYS
        assets = getattr(config, "assets", None) if config is not None else None
        if assets is not None:
            enabled = bool(assets.usage_enabled)
            retention = int(assets.events_retention_days)
        return cls(
            Path(home) / LEDGER_DIR_NAME / LEDGER_FILE_NAME,
            enabled=enabled,
            events_retention_days=retention,
        )

    async def dispose(self) -> None:
        """释放后端共享连接(进程/测试收尾)。

        aiosqlite 的工作线程不是守护线程 —— 不关连接进程会挂住不退出。
        """
        try:
            await self._backend.dispose()
        except Exception as e:
            logger.debug("asset ledger dispose failed: %s", e)

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def _ensure_schema(self) -> None:
        """建表(只成功一次)。

        失败时**不置** ``_schema_ready``:库恢复后下一次落账会自己重试建表。
        代价是坏库期间每次查询都白跑一次建表 —— 换来的是不需要重启进程。
        """
        if self._schema_ready:
            return
        from trove.storage.migrations import POSTGRES, SQLITE, apply_migrations

        is_pg = "Postgres" in type(self._backend).__name__
        await apply_migrations(
            self._backend, STORE_NAME, _migrations(),
            dialect=POSTGRES if is_pg else SQLITE,
        )
        self._schema_ready = True

    # ── 写 ───────────────────────────────────────────────

    async def record(
        self,
        datasource: str,
        asset_key: str,
        *,
        verdict: str,
        elapsed_ms: int | None,
        path: str,
        used_at: str | None = None,
    ) -> None:
        """落一次账。**绝不抛**(I2 / A3)。

        ``elapsed_ms`` 允许 ``None``(没测到),此时明细存 NULL 并排除在分位数
        之外 —— 当成 0 会把中位数一路拽到底,那比"不知道"更糟。

        ``used_at`` 是给测试与补录用的出口,格式必须是 **UTC ISO8601**
        (默认值就是):``purge_events`` 的保留期靠字符串大小比较,混进一个本地
        时区的时间戳,那批行就永远不会被清掉。
        """
        if not self.enabled:
            return
        try:
            verdict = _closed(verdict, VERDICTS, "verdict")
            path = _closed(path, PATHS, "path")
        except ValueError as e:
            # 调用方 bug,不是库挂了 —— 但按 I2 一样不能让它把查询带走。
            # 两者分开记(``reason``),因为处置方向相反:一个是改代码,
            # 一个是修存储。
            metrics.record_asset_ledger_failure(datasource, "invalid")
            logger.warning("asset ledger rejected a record: %s", e)
            return

        stamp = used_at or _utc_now_iso()
        recorded = False
        try:
            conn = await self._conn()
            await conn.execute(
                f"INSERT INTO {EVENTS_TABLE} "
                "(datasource, asset_key, used_at, verdict, elapsed_ms, path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (datasource, asset_key, stamp, verdict, elapsed_ms, path),
            )
            await conn.execute(
                f"INSERT INTO {USAGE_TABLE} "
                "(datasource, asset_key, runs, success_runs, empty_runs, "
                " error_runs, p50_ms, first_used_at, last_used_at) "
                "VALUES (?, ?, 1, ?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT (datasource, asset_key) DO UPDATE SET "
                f"runs = {USAGE_TABLE}.runs + excluded.runs, "
                f"success_runs = {USAGE_TABLE}.success_runs + excluded.success_runs, "
                f"empty_runs = {USAGE_TABLE}.empty_runs + excluded.empty_runs, "
                f"error_runs = {USAGE_TABLE}.error_runs + excluded.error_runs, "
                "last_used_at = excluded.last_used_at",
                (
                    datasource, asset_key,
                    1 if verdict == VERDICT_OK else 0,
                    1 if verdict == VERDICT_EMPTY else 0,
                    1 if verdict == VERDICT_ERROR else 0,
                    # first_used_at 只在**插入那一次**写:上面 DO UPDATE 不碰它,
                    # 这里给的是新行的值。用 COALESCE 反而要在两侧各写一遍。
                    stamp, stamp,
                ),
            )
            await self._recompute_p50(conn, datasource, asset_key)
            await conn.commit()
            recorded = True
        except Exception as e:
            metrics.record_asset_ledger_failure(datasource, "store")
            logger.warning(
                "asset ledger write failed (query unaffected): "
                "datasource=%s asset=%s: %s", datasource, asset_key, e,
            )
        finally:
            # 作用域必须收尾:失败时后端已自行回滚,成功时上面 commit 过 ——
            # 两者都是空操作,但漏掉会让下一个请求等满 60s(见 base.py)。
            try:
                await self._backend.close()
            except Exception:
                pass
        if recorded:
            # 只在**真的提交了**之后记:计时器要回答的是「资产被用了多少次」,
            # 写失败的那些笔不进这个数(它们走 failures 那条)。放在 try 之外,
            # 是为了让"指标自己出问题"不会被误报成"存储挂了"。
            metrics.record_asset_use(datasource, verdict, path)

    # ── 读 ───────────────────────────────────────────────

    async def usage(
        self, datasource: str, keys: Iterable[str],
    ) -> dict[str, AssetUsage]:
        """批量取这些资产的使用情况。**绝不抛**(它在主链路上:治理拿它加权)。

        返回的字典**只含真有行的键** —— 缺席 = 从未用过。失败时返回空字典,
        即设计 §10 的「拿不到统计 → 按无统计处理,不降权」。

        ⚠️ 空字典把「从未用过」和「台账挂了」压成了同一个值。这是有意的:
        两者对治理的结论相同(都不该被降权),让调用方去分辨只是假精度 ——
        真的挂了在 ``trove_asset_ledger_failures_total`` 上看。
        """
        key_list = [k for k in keys if k]
        if not self.enabled or not key_list:
            return {}
        rows: list[Any] = []
        try:
            conn = await self._conn()
            marks = ",".join("?" * len(key_list))
            cursor = await conn.execute(
                f"SELECT asset_key, runs, success_runs, empty_runs, error_runs, "
                f"p50_ms, last_used_at, first_used_at FROM {USAGE_TABLE} "
                f"WHERE datasource = ? AND asset_key IN ({marks})",
                (datasource, *key_list),
            )
            rows = await cursor.fetchall()
        except Exception as e:
            metrics.record_asset_ledger_failure(datasource, "store")
            logger.warning("asset ledger read failed: datasource=%s: %s", datasource, e)
        finally:
            # 读也要收尾。只读不收的话后端的作用域会挂在这个 task 上,下一个
            # 请求要等满 60s 才报错 —— 一次读失败会变成整台机器卡住
            # (见 storage/backends/base.py 与 migrations._end_read)。
            try:
                await self._backend.close()
            except Exception:
                pass
        return {
            row[0]: AssetUsage(
                runs=row[1], success_runs=row[2], empty_runs=row[3],
                error_runs=row[4], p50_ms=row[5],
                last_used_at=row[6], first_used_at=row[7],
            )
            for row in rows
        }

    # ── 维护 ─────────────────────────────────────────────

    async def purge_events(self, older_than_days: int | None = None) -> int:
        """清掉保留期之外的明细,返回删除行数。

        删完必须**重算**受影响资产的 ``p50_ms``:分位数是"保留窗口内"的 p50,
        继续报一个由已删行算出来的值,等于在回答一个已经不存在的问题。
        ``runs`` / ``success_runs`` 等**不回退** —— 那是资产的终身历史,不是
        窗口内统计(从没打算让它随清理缩水)。

        由谁来定期调用属于接线(与 ``_purge_retrieval_log`` 同款),本层只提供
        方法:什么时候清理是部署的决定,不是台账的决定。
        """
        days = self.events_retention_days if older_than_days is None else older_than_days
        if not self.enabled or days <= 0:
            return 0
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)
        ).isoformat()
        try:
            conn = await self._conn()
            # 先取受影响的键再删:删完就查不出"哪些键被删过"了。
            cursor = await conn.execute(
                f"SELECT DISTINCT datasource, asset_key FROM {EVENTS_TABLE} "
                "WHERE used_at < ?", (cutoff,),
            )
            touched = await cursor.fetchall()
            if not touched:
                await conn.close()
                return 0
            cursor = await conn.execute(
                f"DELETE FROM {EVENTS_TABLE} WHERE used_at < ?", (cutoff,),
            )
            removed = cursor.rowcount
            for datasource, key in touched:
                await self._recompute_p50(conn, datasource, key)
            await conn.commit()
            return removed
        except Exception as e:
            metrics.record_asset_ledger_failure("", "store")
            logger.warning("asset ledger purge failed: %s", e)
            return 0
        finally:
            try:
                await self._backend.close()
            except Exception:
                pass

    async def _recompute_p50(self, conn: Any, datasource: str, key: str) -> int | None:
        """从明细重算 p50 并写回聚合行(返回新值,便于测试/复用)。

        在 Python 里取中位而不是 SQL:SQLite 没有 ``PERCENTILE_CONT``,为它写
        两套方言 SQL 不值当,而这个窗口的样本量(单条资产的使用次数)天然不大。

        **偶数样本取下中位**而不是两数平均:取平均会报出一个**从未被观测到**的
        耗时。慢查询长尾里,"这条资产一半以上的调用比 X 慢"这句话必须由真实
        观测支撑。n=1 时它等于那次观测本身 —— 新资产第一次被用就能显示真实
        耗时,而不是四舍五入出来的 0。
        """
        cursor = await conn.execute(
            f"SELECT elapsed_ms FROM {EVENTS_TABLE} "
            "WHERE datasource = ? AND asset_key = ? AND elapsed_ms IS NOT NULL "
            "ORDER BY elapsed_ms",
            (datasource, key),
        )
        values = [row[0] for row in await cursor.fetchall()]
        p50 = values[(len(values) - 1) // 2] if values else None
        await conn.execute(
            f"UPDATE {USAGE_TABLE} SET p50_ms = ? "
            "WHERE datasource = ? AND asset_key = ?",
            (p50, datasource, key),
        )
        return p50


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


__all__ = [
    "AssetLedger",
    "AssetUsage",
    "asset_key",
    "normalize_question",
    "DEFAULT_EVENTS_RETENTION_DAYS",
    "EVENTS_TABLE",
    "PATH_FAST",
    "PATH_RETRIEVAL",
    "PATHS",
    "STORE_NAME",
    "USAGE_TABLE",
    "VERDICT_EMPTY",
    "VERDICT_ERROR",
    "VERDICT_OK",
    "VERDICTS",
]
