"""AssetLedger 测试 —— 落账 / 分位数 / 资产主键稳定性(P2)。

对应设计稿 2026-09-28-verified-query-assets-design.md 的 §6.2(表结构)、
§6.3(主键)、§12 验收:A3(落账失败不影响查询返回)、A8(统计零 LLM)、
A9(``asset_key`` 对措辞微调稳定)。

测试名写的是**被钉住的行为**,不是接口名 —— 名字里出现 fail/stable/absent
的地方,就是那条不变量被人改坏时先红的地方。
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

from trove.services.kb import ledger as ledger_mod
from trove.services.kb.ledger import (
    PATHS,
    PATH_FAST,
    PATH_RETRIEVAL,
    STORE_NAME,
    VERDICT_EMPTY,
    VERDICT_ERROR,
    VERDICT_OK,
    VERDICTS,
    AssetLedger,
    asset_key,
    normalize_question,
)
from trove.services.kb.ledger import _closed

DS = "demo"


@pytest.fixture
async def ledger(tmp_path):
    """文件库的 ledger(不用 :memory: —— 测试要能从**外面**看这张库)。"""
    led = AssetLedger(tmp_path / "asset_ledger.sqlite")
    yield led
    await led.dispose()


def _iso(seconds_ago: float = 0.0) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=seconds_ago)
    ).isoformat()


async def _insert_event(
    db_path: str | Path,
    datasource: str,
    key: str,
    *,
    elapsed_ms: int | None,
    verdict: str = VERDICT_OK,
    path: str = PATH_FAST,
) -> None:
    """绕过 ledger 直接往明细表写一行(模拟第二个写入者 / 聚合漂移)。"""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO asset_usage_events "
            "(datasource, asset_key, used_at, verdict, elapsed_ms, path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datasource, key, _iso(), verdict, elapsed_ms, path),
        )
        await db.commit()


async def _scalar(db_path: str | Path, sql: str, params: tuple = ()):
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
    return row[0] if row else None


def _counter(name: str, **labels: str) -> float:
    """从 Prometheus 文本暴露格式里读一条 series 的值(没有 = 0)。

    走 ``generate_latest`` 而不是碰 ``Counter._value``:文本格式是 Prometheus
    的**公开契约**,私有属性不是。
    """
    from prometheus_client import generate_latest

    from trove.core import metrics as metrics_mod

    body = generate_latest(metrics_mod._REGISTRY).decode()
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        head, _, value = line.rpartition(" ")
        found = dict(re.findall(r'(\w+)="([^"]*)"', head))
        if all(found.get(k) == v for k, v in labels.items()):
            return float(value)
    return 0.0


class _DeadBackend:
    """注入用的坏 store:任何一次调用都抛(ledger 所在库整个不可用)。

    有 ``rollback_pending`` —— 即 ``migrations._is_backend`` 认它是
    ``StorageBackend``,所以 ``_end_read`` 也会去调它的 ``close()``:注入的
    失败必须**每一条路径**都抛,才是真的"库挂了"。
    """

    def __init__(self) -> None:
        self.calls = 0

    def _boom(self):
        self.calls += 1
        raise RuntimeError("ledger store is down")

    async def execute(self, *_a, **_kw):
        self._boom()

    async def commit(self) -> None:
        self._boom()

    async def close(self) -> None:
        self._boom()

    async def executescript(self, *_a, **_kw) -> None:
        self._boom()

    async def rollback_pending(self) -> None:
        self._boom()

    async def dispose(self) -> None:
        self._boom()


# ── 落账与聚合 ───────────────────────────────────────────


async def test_record_then_usage_reports_one_run(ledger):
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=120, path=PATH_FAST
    )
    got = await ledger.usage(DS, ["k1"])
    assert got["k1"].runs == 1
    assert got["k1"].success_runs == 1
    assert got["k1"].empty_runs == 0
    assert got["k1"].error_runs == 0


async def test_runs_always_equals_sum_of_the_three_verdict_buckets(ledger):
    """四个计数必须自洽 —— 不自洽的话「runs=47 但分桶加起来 31」无人能解释。"""
    for verdict, n in (
        (VERDICT_OK, 3), (VERDICT_EMPTY, 2), (VERDICT_ERROR, 4),
    ):
        for _ in range(n):
            await ledger.record(
                DS, "k1", verdict=verdict, elapsed_ms=10, path=PATH_RETRIEVAL
            )
    u = (await ledger.usage(DS, ["k1"]))["k1"]
    assert u.runs == 9
    assert u.runs == u.success_runs + u.empty_runs + u.error_runs
    assert (u.success_runs, u.empty_runs, u.error_runs) == (3, 2, 4)


async def test_recording_twice_accumulates_two_runs(ledger):
    """重复记录是**追加明细**,不是撞 UNIQUE —— 聚合行被 upsert,不报错。"""
    for _ in range(2):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=5, path=PATH_FAST
        )
    assert (await ledger.usage(DS, ["k1"]))["k1"].runs == 2


async def test_runs_in_the_aggregate_matches_the_event_row_count(ledger):
    """`asset_usage` 是明细表的物化视图 —— 两边的口径必须一样。"""
    for ms in (10, 20, 30):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=ms, path=PATH_FAST
        )
    events = await _scalar(
        ledger.db_path,
        "SELECT COUNT(*) FROM asset_usage_events WHERE datasource=? AND asset_key=?",
        (DS, "k1"),
    )
    agg = await _scalar(
        ledger.db_path,
        "SELECT runs FROM asset_usage WHERE datasource=? AND asset_key=?",
        (DS, "k1"),
    )
    assert events == agg == 3


async def test_an_unknown_verdict_never_reaches_the_store(ledger):
    """verdict 是闭集 —— 放进去一个不认识的,四个计数就不再自洽了。

    异常在 ``record()`` 里被 I2 收掉(调用方是查询路径),所以这里断言的是
    **它没落账**,而"为什么没落"由失败计数器回答(下一条测试)。
    """
    await ledger.record(
        DS, "k1", verdict="SUCCESS", elapsed_ms=1, path=PATH_FAST
    )
    assert "k1" not in await ledger.usage(DS, ["k1"])


def test_the_ledger_enums_are_the_metric_label_domains():
    """台账的枚举与指标接受的标签值必须是**同一份契约**。

    漂移的表现是「落了账但指标没记」—— 一个从两个方向都查不出来的缺口:
    看指标以为没被用过,看台账又明明有记录。
    """
    from trove.core import metrics as metrics_mod

    assert VERDICTS == metrics_mod.ASSET_LEDGER_VERDICTS
    assert PATHS == metrics_mod.ASSET_LEDGER_PATHS
    assert VERDICTS == {VERDICT_OK, VERDICT_EMPTY, VERDICT_ERROR}


def test_closed_set_lookup_returns_the_canonical_spelling():
    """查表返回集合里的拼法,不是输入的大写 —— 两个集合的规范拼法不同
    (verdict 大写、path 小写),统一折向一边会把另一边的值全判成非法。"""
    assert _closed(" ok ", VERDICTS, "verdict") == VERDICT_OK
    assert _closed("Fast_Path", PATHS, "path") == PATH_FAST
    with pytest.raises(ValueError):
        _closed("SUCCESS", VERDICTS, "verdict")


async def test_unknown_verdict_is_dropped_without_counting(ledger):
    """大小写/空白只是**形式**,归一化后照收;换一个词是**身份**不同,不收。"""
    await ledger.record(
        DS, "k1", verdict=" ok ", elapsed_ms=1, path=PATH_FAST
    )
    assert (await ledger.usage(DS, ["k1"]))["k1"].runs == 1

    await ledger.record(
        DS, "k2", verdict="SUCCESS", elapsed_ms=1, path=PATH_FAST
    )
    assert "k2" not in await ledger.usage(DS, ["k2"])


async def test_unknown_path_is_dropped_without_counting(ledger):
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path="telepathy"
    )
    assert "k1" not in await ledger.usage(DS, ["k1"])


# ── 读侧:缺行 / 隔离 / 批量 ─────────────────────────────


async def test_unused_key_is_absent_rather_than_zero_filled(ledger):
    """从没用过 = **没有行**。回一行 runs=0 等于宣称"我们统计过,确实是 0"。"""
    await ledger.record(
        DS, "used", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    got = await ledger.usage(DS, ["used", "never-used"])
    assert set(got) == {"used"}


async def test_usage_is_scoped_per_datasource(ledger):
    """同一句问句在不同库上是**不同资产**(设计 §3.2 N3:不做跨源共享)。"""
    key = asset_key("How many accounts are there?")
    await ledger.record(
        "demo", key, verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    await ledger.record(
        "financial", key, verdict=VERDICT_ERROR, elapsed_ms=1, path=PATH_FAST
    )
    assert (await ledger.usage("demo", [key]))[key].success_runs == 1
    assert (await ledger.usage("financial", [key]))[key].error_runs == 1


async def test_usage_batches_several_keys_in_one_call(ledger):
    for k in ("a", "b", "c"):
        await ledger.record(
            DS, k, verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
        )
    got = await ledger.usage(DS, ["a", "b", "c", "d"])
    assert set(got) == {"a", "b", "c"}


async def test_usage_with_no_keys_does_not_touch_the_store(ledger):
    """空候选是常见情况(检索没召回),不该白跑一次查询。"""
    assert await ledger.usage(DS, []) == {}


async def test_first_and_last_used_at_bracket_the_window(ledger):
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST,
        used_at=_iso(600),
    )
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST,
        used_at=_iso(0),
    )
    u = (await ledger.usage(DS, ["k1"]))["k1"]
    assert u.first_used_at < u.last_used_at
    assert u.first_used_at.endswith("+00:00")


# ── 分位数:从明细表算 ───────────────────────────────────


async def test_p50_of_a_single_run_is_that_run(ledger):
    """一个新资产第一次被用,它显示的应该是**真实耗时**,不是 0。"""
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=320, path=PATH_FAST
    )
    assert (await ledger.usage(DS, ["k1"]))["k1"].p50_ms == 320


async def test_p50_takes_the_lower_middle_of_an_even_sample(ledger):
    """偶数样本取下中位:取平均会报出一个**从未被观测到**的耗时。"""
    for ms in (100, 200, 300, 400):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=ms, path=PATH_FAST
        )
    assert (await ledger.usage(DS, ["k1"]))["k1"].p50_ms == 200


async def test_p50_follows_the_detail_table_not_a_running_average(ledger):
    """明细是唯一的真相来源。

    直接往明细表塞一行(聚合行不知道这件事),下一次落账重算时必须把它算进去。
    若是"把新耗时和上一次的 p50 平均一下"这类增量写法,结果会是 ~633。
    """
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=100, path=PATH_FAST
    )
    await _insert_event(ledger.db_path, DS, "k1", elapsed_ms=900)
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=900, path=PATH_FAST
    )
    # 明细 = [100, 900, 900],下中位 = 900
    assert (await ledger.usage(DS, ["k1"]))["k1"].p50_ms == 900


async def test_p50_ignores_unmeasured_runs_instead_of_treating_them_as_zero(ledger):
    """没测到耗时 = NULL。当成 0 会把中位数一路拽到底。"""
    for ms in (400, 800):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=ms, path=PATH_FAST
        )
    for _ in range(5):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=None, path=PATH_FAST
        )
    u = (await ledger.usage(DS, ["k1"]))["k1"]
    assert u.runs == 7
    assert u.p50_ms == 400
    # 明细里存的确实是 NULL,不是 0
    assert await _scalar(
        ledger.db_path,
        "SELECT COUNT(*) FROM asset_usage_events "
        "WHERE datasource=? AND asset_key=? AND elapsed_ms IS NULL",
        (DS, "k1"),
    ) == 5


async def test_p50_is_absent_when_no_run_was_measured(ledger):
    """一次都没测到 → ``None``,不是 0。慢/快无从谈起。"""
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=None, path=PATH_FAST
    )
    assert (await ledger.usage(DS, ["k1"]))["k1"].p50_ms is None


# ── I2 / A3:落账失败不影响主链路 ────────────────────────


async def test_record_failure_does_not_propagate(tmp_path):
    """A3:库从头就是坏的 —— 调用方(查询路径)绝不能看到异常。"""
    led = AssetLedger(tmp_path / "asset_ledger.sqlite")
    dead = _DeadBackend()
    led._backend = dead
    await led.record(  # 不抛
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    assert dead.calls > 0


async def test_record_failure_after_schema_ready_does_not_propagate(ledger):
    """建表成功、写入时才坏 —— 这是生产里更常见的那一种(磁盘满 / 连接断)。"""
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    ledger._backend = _DeadBackend()
    await ledger.record(  # 不抛
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )


async def test_usage_failure_does_not_propagate(ledger):
    """读侧也在主链路上(治理要拿统计去加权),同样不能抛。

    失败 = 空字典 = 设计 §10 的「拿不到统计 → 按无统计处理,不降权」。
    """
    ledger._backend = _DeadBackend()
    assert await ledger.usage(DS, ["k1"]) == {}


async def test_ledger_failure_is_visible_in_metrics(tmp_path):
    """I2 让失败对调用方不可见 —— 指标是它唯一还能被看见的地方。"""
    before = _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="store")
    led = AssetLedger(tmp_path / "asset_ledger.sqlite")
    led._backend = _DeadBackend()
    await led.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    assert _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="store",
    ) == before + 1


async def test_rejected_record_is_reported_apart_from_a_broken_store(ledger):
    """库坏了要修存储,传错 verdict 要改代码 —— 混成一个数就读不出该动哪边。"""
    before_store = _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="store")
    before_invalid = _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="invalid")
    await ledger.record(
        DS, "k1", verdict="NOPE", elapsed_ms=1, path=PATH_FAST
    )
    assert _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="invalid",
    ) == before_invalid + 1
    assert _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="store",
    ) == before_store


async def test_successful_record_does_not_count_a_failure(ledger):
    before = _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="store")
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    assert _counter(
        "trove_asset_ledger_failures_total", datasource=DS, reason="store",
    ) == before


# ── 指标:基数纪律 ────────────────────────────────────────


async def test_runs_counter_carries_only_datasource_verdict_path(ledger):
    """question / asset_key / SQL 一律不进标签 —— 基数是无限的(见 metrics.py)。"""
    before = _counter(
        "trove_asset_runs_total", datasource=DS, verdict=VERDICT_OK, path=PATH_FAST
    )
    await ledger.record(
        DS, asset_key("How many accounts?"),
        verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST,
    )
    assert _counter(
        "trove_asset_runs_total", datasource=DS, verdict=VERDICT_OK, path=PATH_FAST
    ) == before + 1
    # 问句与主键都不该在标签里出现
    from prometheus_client import generate_latest

    from trove.core import metrics as metrics_mod

    body = generate_latest(metrics_mod._REGISTRY).decode()
    assert "How many accounts" not in body


# ── A9:资产主键 ──────────────────────────────────────────


async def test_asset_key_is_stable_under_wording_tweaks():
    """A9:确认流程会重写 question 措辞(标点/大小写/空白),主键必须扛得住。

    扛不住 = 每改写一次就把这条资产的 `runs` 清零重来。
    """
    base = asset_key("How many records are in the account table?")
    for variant in (
        "how many records are in the account table",
        "  How many records are in the account table?  ",
        "How  many   records are in the account table?",
        "How many records are in the account table.",
        "How many records, are in the account table",
    ):
        assert asset_key(variant) == base, variant


async def test_asset_key_is_stable_for_cjk_wording_tweaks():
    base = asset_key("账户表里有多少条记录？")
    assert asset_key("账户表里有多少条记录") == base
    assert asset_key("  账户表里有多少条记录  ") == base


async def test_asset_key_keeps_distinct_questions_apart():
    assert asset_key("How many accounts?") != asset_key("How many orders?")


async def test_asset_key_keeps_underscore_apart_from_space():
    """``account_id`` 与 ``account id`` 是**两个**资产。

    这是与 ``trove/eval/baseline.py`` 的 ``_norm`` 有意不同的地方:那个是评测
    对账键,把 ``_`` 一起折成空格;这里折了会把「问列名」和「问自然语言」并成
    一条 —— 少记一条只是多一行,合并错一条是统计串台。
    """
    assert asset_key("what is account_id") != asset_key("what is account id")


async def test_asset_key_is_a_16_char_hex_digest():
    k = asset_key("How many records are in the account table?")
    assert len(k) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", k)


def test_asset_key_of_an_empty_question_is_still_a_digest():
    """空问句不该抛 —— 它是可哈希的输入,不是异常(键唯一即可,是否出现是调用方的事)。"""
    assert len(asset_key("")) == 16


def test_normalize_matches_the_session_question_normalizer():
    """不许长出第三套问句归一化。

    ``SessionManager._normalize_question`` 已经在用同一套算法做**结果缓存**的
    键 —— 两处若漂移,同一句问句在缓存和台账里会是两个身份。
    """
    from trove.agent.session import SessionManager

    for q in (
        "How many records are in the account table?",
        "  what's  the   average  ",
        "账户表里有多少条记录？",
        "SELECT * FROM t WHERE a=1",
        "",
    ):
        assert normalize_question(q) == SessionManager._normalize_question(q)


# ── A8 / I7:统计零 LLM ──────────────────────────────────


def test_ledger_module_declares_no_llm_dependency():
    """统计是纯 DB 写入 —— 模块里不该有任何 LLM 导入(含函数内的惰性导入)。"""
    src = Path(ledger_mod.__file__).read_text(encoding="utf-8")
    imported = re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", src, re.M)
    assert not [m for m in imported if m == "llm" or m.startswith("trove.llm")]


async def test_full_record_and_usage_cycle_runs_without_any_llm(tmp_path):
    """A8:不注入 LLM 也能跑通整条链路 —— 并且整条链路**没有把 LLM 拉进来**。"""
    led = AssetLedger(tmp_path / "asset_ledger.sqlite")
    try:
        await led.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
        )
        assert (await led.usage(DS, ["k1"]))["k1"].runs == 1
    finally:
        await led.dispose()


def test_importing_the_ledger_pulls_in_no_llm_module(tmp_path):
    """子进程里从零导入并跑一遍,断言 ``sys.modules`` 里没有 ``trove.llm``。

    比扫源码强:它覆盖**传递**依赖 —— 只要有中间模块顺手把 LLM 拖进来,这里就红。
    """
    # 脚本里必须显式 dispose:aiosqlite 的工作线程不是守护线程,不关连接
    # 进程会挂住不退出(子进程超时,报出来的却是"零 LLM 断言失败"的假象)。
    code = (
        "import asyncio, sys\n"
        "from trove.services.kb.ledger import AssetLedger\n"
        "\n"
        "async def main():\n"
        "    led = AssetLedger(r'%s')\n"
        "    await led.record('demo', 'k', verdict='OK', elapsed_ms=1,"
        " path='fast_path')\n"
        "    assert (await led.usage('demo', ['k']))['k'].runs == 1\n"
        "    await led.dispose()\n"
        "\n"
        "asyncio.run(main())\n"
        "print(sorted(m for m in sys.modules if m.startswith('trove.llm')))\n"
    ) % (tmp_path / "sub.sqlite")
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(Path(ledger_mod.__file__).parents[3]), timeout=180,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ── 存储 / 迁移 / 配置 ───────────────────────────────────


async def test_ledger_records_its_schema_version(ledger):
    """库要记得"我升到哪一版" —— 顺带证明它走的是 migrations 那套,不是每次探测。"""
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
    )
    assert await _scalar(
        ledger.db_path,
        "SELECT version FROM trove_schema WHERE store = ?",
        (STORE_NAME,),
    ) == 1


async def test_a_database_from_a_newer_trove_is_refused_but_never_fails_a_query(
    tmp_path,
):
    """库比代码新 → 拒绝写(会静默丢数据),但按 I2 仍然不抛。

    这正是失败计数器存在的原因:拒绝是**静默**的,只能从指标上看见。
    """
    db = tmp_path / "asset_ledger.sqlite"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "CREATE TABLE trove_schema (store TEXT PRIMARY KEY, "
            "version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO trove_schema VALUES (?, ?, ?)",
            (STORE_NAME, 99, _iso()),
        )
        await conn.commit()

    led = AssetLedger(db)
    try:
        await led.record(  # 不抛
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
        )
    finally:
        await led.dispose()
    # 一行都没写进去(表根本没建)
    assert await _scalar(
        db,
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='asset_usage_events'",
    ) == 0


async def test_disabled_ledger_records_nothing(tmp_path):
    """``agent.assets.usage_enabled: false`` —— 统计整体关掉时不留半张表。"""
    led = AssetLedger(tmp_path / "asset_ledger.sqlite", enabled=False)
    try:
        await led.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=1, path=PATH_FAST
        )
        assert await led.usage(DS, ["k1"]) == {}
    finally:
        await led.dispose()


def test_for_home_puts_the_ledger_beside_the_kb_not_inside_it(tmp_path):
    """台账落在 ``<home>/assets/``,**不**在 ``<home>/kb/`` 里。

    kb 目录是被 ``git_versioning`` 版本化的资产目录 —— 高频统计写进去会把它
    淹在噪声里(设计 §6.2 选择独立存储的同一个理由)。
    """
    led = AssetLedger.for_home(tmp_path)
    assert Path(led.db_path) == tmp_path / "assets" / "asset_ledger.sqlite"
    assert "kb" not in Path(led.db_path).parts


async def test_for_home_reads_agent_assets_config(tmp_path):
    from trove.core.config import AgentConfig

    cfg = AgentConfig()
    cfg.assets.usage_enabled = False
    led = AssetLedger.for_home(tmp_path, config=cfg)
    assert led.enabled is False


async def test_purge_drops_old_events_and_keeps_recent_ones(ledger):
    """明细表只增不减会长到没人敢动 —— 保留期是它的出口。"""
    for ago in (60 * 60 * 24 * 200, 60 * 60 * 24 * 30):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=100, path=PATH_FAST,
            used_at=_iso(ago),
        )
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=200, path=PATH_FAST,
        used_at=_iso(10),
    )
    removed = await ledger.purge_events(older_than_days=90)
    assert removed == 1  # 只有 200 天前那条在窗口外,30 天前那条要留着
    assert await _scalar(
        ledger.db_path,
        "SELECT COUNT(*) FROM asset_usage_events WHERE datasource=? AND asset_key=?",
        (DS, "k1"),
    ) == 2


async def test_purge_recomputes_p50_so_it_never_quotes_deleted_rows(ledger):
    """p50 是**保留窗口内**的 p50:删掉旧明细后必须重算,不能继续报旧值。"""
    for ms in (900, 900):
        await ledger.record(
            DS, "k1", verdict=VERDICT_OK, elapsed_ms=ms, path=PATH_FAST,
            used_at=_iso(60 * 60 * 24 * 200),
        )
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=50, path=PATH_FAST,
        used_at=_iso(10),
    )
    assert (await ledger.usage(DS, ["k1"]))["k1"].p50_ms == 900

    await ledger.purge_events(older_than_days=90)

    u = (await ledger.usage(DS, ["k1"]))["k1"]
    assert u.p50_ms == 50
    # runs 是**终身**计数,不随明细清理回退 —— 它是资产的历史,不是窗口内统计
    assert u.runs == 3


async def test_purge_keeps_the_aggregate_when_every_event_is_gone(ledger):
    await ledger.record(
        DS, "k1", verdict=VERDICT_OK, elapsed_ms=100, path=PATH_FAST,
        used_at=_iso(60 * 60 * 24 * 200),
    )
    await ledger.purge_events(older_than_days=90)
    u = (await ledger.usage(DS, ["k1"]))["k1"]
    assert u.runs == 1
    assert u.p50_ms is None
