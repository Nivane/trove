"""Live declaration verification — semantic model claims vs the connected DB.

The semantic model's accuracy levers all rest on one assumption nobody
checks: that the declarations are *true*. A natural key that duplicates, a
``1:N`` edge whose "one" side is not unique, a ``fan_out: dedup`` exemption
whose duplicates are not exact rows — each flips a guard into its opposite
(fan-out gating multiplies rows instead of preventing it, skeleton fidelity
asserts a false premise), and none of it fails loudly at runtime: the query
just returns a plausible wrong number.

This module is the falsifier. Every probe is a read-only ``COUNT`` against
the declared physical table; zero LLM. Verdicts are a closed four, none of
them silent:

- ``verified``     — the claim holds on today's data (counts in message);
- ``violated``     — the claim is false *now*;
- ``advisory``     — the claim holds, but the data says something worth a
                     look (NULL keys, nothing to dedup, an unimplemented
                     exemption);
- ``unverifiable`` — the probe could not run. **判不了 ≠ 通过** — it is
                     reported, never counted as verified.

Scope discipline: only *declared* things are probed. An undeclared key or
edge is the coverage report's business (``_coverage`` — advisories, zero
queries), so an entity-less model costs nothing but its coverage lines and
this module never invents a claim the author did not make.

Probes need backtick-quoted identifiers (mysql / sqlite / duckdb /
clickhouse / doris); other dialects get one honest ``unverifiable`` instead
of N fake alarms — the same call as ``kb.columns``' MySQL-only gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticModel,
    SemanticRelationship,
)

VERIFIED = "verified"
VIOLATED = "violated"
ADVISORY = "advisory"
UNVERIFIABLE = "unverifiable"

#: 探测用反引号引用标识符;其余方言给一条诚实的"跳过"(与 kb.columns
#: 的 MySQL-only 闸同款:逐条报 SQL 错只会刷屏)。
_QUOTED_DIALECTS = frozenset(
    {"mysql", "sqlite", "duckdb", "clickhouse", "doris"})


@dataclass(frozen=True)
class Probe:
    """One verdict on one declared claim.

    ``kind`` doubles as the validate check-id suffix (``semantic.keys`` /
    ``semantic.relationships`` / ``semantic.fan_out`` / ``semantic.coverage``
    / ``semantic.verify``); ``target`` is the dataset / relationship name the
    claim was made on.
    """

    kind: str
    target: str
    verdict: str
    message: str


def _q(part: str) -> str:
    return "`" + str(part).replace("`", "``") + "`"


def _table_ref(source: str) -> str:
    return ".".join(_q(p) for p in str(source).split(".") if p)


def _cols(cols: list[str]) -> str:
    return ", ".join(_q(c) for c in cols)


def _not_null(cols: list[str]) -> str:
    return " AND ".join(f"{_q(c)} IS NOT NULL" for c in cols)


async def _scalar(adapter, sql: str) -> int:
    rows = (await adapter.execute(sql)).rows
    return int(rows[0][0])


async def verify_declarations(
    adapter, model: SemanticModel, dialect: str,
) -> list[Probe]:
    """Every probe for one semantic model. Read-only; per-probe failures are
    verdicts (``unverifiable``), not exceptions."""
    if dialect not in _QUOTED_DIALECTS:
        return [Probe(
            "verify", "", UNVERIFIABLE,
            f"声明验证需要反引号引用标识符,本次 dialect={dialect or '未知'},跳过")]
    probes: list[Probe] = []
    by_name = {d.name: d for d in model.datasets}
    for ds in model.datasets:
        probes += await _verify_dataset_key(adapter, ds)
    for rel in model.relationships:
        probes += await _verify_relationship(adapter, rel, by_name)
    probes += _coverage(model)
    return probes


async def _verify_dataset_key(adapter, ds: SemanticDataset) -> list[Probe]:
    """Entity natural key (or the primary-key fallback) vs the live table.

    Only *declared* keys are probed — a dataset that declared neither is a
    coverage finding, not a probe.

    NULL semantics are load-bearing: SQL uniqueness does not dedupe NULLs
    (and ``COUNT(DISTINCT …)`` skips them), so the uniqueness claim is
    judged on the non-NULL rows alone and NULL keys are their own advisory.
    Without the split, two NULL-keyed rows would read as a duplicate-key
    violation — a false alarm about a real data fact.
    """
    entity = ds.entity
    if entity is not None and entity.natural_keys:
        keys, label = list(entity.natural_keys), "自然键"
    elif ds.primary_key:
        keys, label = list(ds.primary_key), "主键"
    else:
        return []
    target = f"{ds.name}.{'+'.join(keys)}"
    if not ds.source:
        return [Probe("keys", target, UNVERIFIABLE,
                      f"数据集「{ds.name}」未声明物理来源(source),{label}无法探测")]
    table = _table_ref(ds.source)
    cols, notnull = _cols(keys), _not_null(keys)
    try:
        total = await _scalar(adapter, f"SELECT COUNT(*) FROM {table}")
        nulls = await _scalar(
            adapter,
            f"SELECT COUNT(*) FROM {table} WHERE "
            + " OR ".join(f"{_q(c)} IS NULL" for c in keys))
        clean = total - nulls
        distinct = await _scalar(
            adapter,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {table}"
            f" WHERE {notnull}) AS _dk")
    except Exception as e:  # noqa: BLE001 — 探测失败如实报,不当通过
        return [Probe("keys", target, UNVERIFIABLE,
                      f"{label}唯一性探测失败: {e}")]
    out: list[Probe] = []
    if distinct < clean:
        out.append(Probe(
            "keys", target, VIOLATED,
            f"{label}不唯一: {ds.source} 非空键 {clean} 行去重后只剩 "
            f"{distinct}(重复 {clean - distinct} 行)—— 声明与数据不一致,"
            f"键锚定与骨架保真保的是一个不成立的前提"))
    else:
        out.append(Probe("keys", target, VERIFIED,
                         f"{label}唯一(非空 {clean} 行)"))
    if nulls:
        out.append(Probe(
            "keys", target, ADVISORY,
            f"{label}含 NULL {nulls} 行 —— SQL 唯一语义对 NULL 不去重,"
            f"这些行不能被键锚定"))
    return out


async def _verify_relationship(
    adapter, rel: SemanticRelationship, by_name: dict[str, SemanticDataset],
) -> list[Probe]:
    """The verifiable claim of one edge: the "one" side is unique.

    many→one means every ``from`` row matches at most one ``to`` row — so
    ``to_columns`` must be unique in the ``to`` table. That is the whole
    cardinality claim, and it is exactly what a wrong declaration (or a
    flipped direction) breaks: joins through the edge multiply rows, and
    which of SUM and SUM-of-fanout is the answer quietly changes.

    ``M:N`` declares no uniqueness (the compiler refuses joins through it
    unless a ``fan_out`` exemption is declared) — its only falsifiable part
    is the exemption, probed separately. ``1:1`` adds the ``from`` side.
    """
    src, dst = by_name.get(rel.from_), by_name.get(rel.to)
    if src is None or dst is None:
        missing = [n for n, d in ((rel.from_, src), (rel.to, dst)) if d is None]
        return [Probe("relationships", rel.name, UNVERIFIABLE,
                      f"引用的数据集未声明: {', '.join(missing)},边无法探测")]
    if (not rel.from_columns or not rel.to_columns
            or len(rel.from_columns) != len(rel.to_columns)):
        return [Probe("relationships", rel.name, UNVERIFIABLE,
                      "键对缺失或长度不一致,边无法探测")]
    probes: list[Probe] = []
    if rel.cardinality != "M:N":
        probes += await _probe_side_unique(
            adapter, rel.name, dst, rel.to_columns, label="to 侧(「一」侧)键")
        if rel.cardinality == "1:1":
            probes += await _probe_side_unique(
                adapter, rel.name, src, rel.from_columns, label="from 侧键(1:1)")
    if rel.fan_out == "dedup":
        probes += await _probe_dedup(adapter, rel, src)
    elif rel.fan_out.startswith("bridge:"):
        # 编译期对 bridge 是「保留未实现 → 仍拒」(compiler.py 的 fan_out
        # 分支)—— 一条看起来放行了、实际什么都没放行的豁免,正是 validate
        # 要抓的死配置。
        probes.append(Probe(
            "fan_out", rel.name, ADVISORY,
            f"fan_out={rel.fan_out} 为保留语义,编译期未实现该豁免 —— "
            f"经该边的连接仍按 fan-out 拒绝,豁免当前不生效"))
    return probes


async def _probe_side_unique(
    adapter, rel_name: str, ds: SemanticDataset, cols: list[str], *,
    label: str,
) -> list[Probe]:
    if not ds.source:
        return [Probe("relationships", rel_name, UNVERIFIABLE,
                      f"数据集「{ds.name}」未声明物理来源(source),{label}无法探测")]
    table = _table_ref(ds.source)
    notnull = _not_null(cols)
    try:
        clean = await _scalar(
            adapter, f"SELECT COUNT(*) FROM {table} WHERE {notnull}")
        distinct = await _scalar(
            adapter,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {_cols(cols)} FROM {table}"
            f" WHERE {notnull}) AS _dk")
    except Exception as e:  # noqa: BLE001
        return [Probe("relationships", rel_name, UNVERIFIABLE,
                      f"{label}唯一性探测失败: {e}")]
    if distinct < clean:
        return [Probe(
            "relationships", rel_name, VIOLATED,
            f"{label}不唯一: {ds.source} 非空键 {clean} 行去重后 {distinct}"
            f"(重复 {clean - distinct} 行)—— 声明的 many→one 不成立,"
            f"经该边连接会行倍增;若确为多对多,应改声明 M:N")]
    return [Probe("relationships", rel_name, VERIFIED, f"{label}唯一({clean} 行)")]


async def _probe_dedup(
    adapter, rel: SemanticRelationship, src: SemanticDataset,
) -> list[Probe]:
    """``fan_out: dedup`` 的可证伪前提:DISTINCT * 之后每键恰一行。

    dedup 唯一安全的情形是 from 侧的重复为**整行精确重复**(关联/桥接表
    的整行复制);同键但非整行相同的多行,DISTINCT * 去不掉 —— 豁免看着
    放行了,行倍增照旧发生。两个口径都按 SQL DISTINCT 的 NULL 分组语义
    数(含 NULL 键),不掺 NOT NULL 过滤:混口径会把 NULL 键的行虚构成本来
    没有的重复。
    """
    if not src.source:
        return [Probe("fan_out", rel.name, UNVERIFIABLE,
                      f"数据集「{src.name}」未声明物理来源(source),dedup 前提无法探测")]
    table = _table_ref(src.source)
    cols = _cols(rel.from_columns)
    try:
        total = await _scalar(adapter, f"SELECT COUNT(*) FROM {table}")
        distinct_rows = await _scalar(
            adapter,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT * FROM {table}) AS _dr")
        distinct_keys = await _scalar(
            adapter,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {table}) AS _dk")
    except Exception as e:  # noqa: BLE001
        return [Probe("fan_out", rel.name, UNVERIFIABLE,
                      f"dedup 前提探测失败: {e}")]
    if distinct_rows > distinct_keys:
        return [Probe(
            "fan_out", rel.name, VIOLATED,
            f"dedup 豁免不成立: FROM 侧 {src.source} 折叠精确重复行后仍有 "
            f"{distinct_rows - distinct_keys} 个同键不同行 —— DISTINCT * "
            f"去不掉它们,经该边连接仍会行倍增")]
    if total == distinct_rows:
        return [Probe(
            "fan_out", rel.name, ADVISORY,
            f"FROM 侧 {src.source} 当前没有精确重复行 —— dedup 豁免无实际"
            f"作用,可复核是否仍需保留(数据阶段或豁免冗余)")]
    return [Probe(
        "fan_out", rel.name, VERIFIED,
        f"dedup 成立: {total} 行折叠 {total - distinct_rows} 个精确重复后每键一行")]


def _coverage(model: SemanticModel) -> list[Probe]:
    """声明覆盖率 —— 纯投影、零查询:哪些数据集没实体、哪些对象没键、
    哪些数据集之间一条边都没有。

    都是 advisory(未声明不是错,是还没做),进同一份报告让「本体做了
    多少」有一个可读的数。只报声明层的事实,不对库做任何探测 ——
    未声明的东西没有可证伪的声明,不该产生对库查询。
    """
    out: list[Probe] = []
    for ds in model.datasets:
        if ds.entity is None:
            out.append(Probe(
                "coverage", ds.name, ADVISORY,
                f"数据集「{ds.name}」未声明实体 —— 对象词汇/别名词法锚定不生效"))
        elif not ds.entity.natural_keys and not ds.primary_key:
            out.append(Probe(
                "coverage", ds.name, ADVISORY,
                f"数据集「{ds.name}」未声明任何键(自然键/主键)—— "
                f"唯一性与骨架保真无声明可用"))
    if len(model.datasets) >= 2 and not model.relationships:
        out.append(Probe(
            "coverage", model.name or "semantic_model", ADVISORY,
            f"{len(model.datasets)} 个数据集之间未声明任何关系 —— "
            f"跨表连接只能走命名推断"))
    return out
