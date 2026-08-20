"""Deterministic SQL lineage extraction (SQLGlot, zero IO, zero LLM).

Every lineage fact derives from a SQL string: what tables/columns a query
reads, and best-effort column-level mapping between a projection output
and its source columns. Views / CREATE TABLE AS SELECT register as
producers; plain SELECT/DDL are consumed as query digest (recorded as
downstream usage).

Parse is best-effort by design: unparseable SQL or exotic dialects return
None so the service never blocks on a malformed definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


@dataclass
class ColumnSource:
    """One contributing source column for a projected output column."""

    table: str  # base table name ('' = ambiguous/unknown)
    column: str
    expr: str  # trimmed expression text this source appears in


@dataclass
class QueryDigest:
    """Structured lineage facts for one SQL statement."""

    kind: str  # 'query' | 'create_view' | 'create_table_as'
    name: str  # produced table/view name ('' for plain queries)
    tables_read: list[str] = field(default_factory=list)  # deduped base tables
    columns_read: list[tuple[str, str]] = field(default_factory=list)  # (table, col)
    outputs: list[tuple[str, list[ColumnSource]]] = field(default_factory=list)


def _base_aliases(select: exp.Select) -> dict[str, str]:
    """Map alias → base table name for FROM/JOIN in a SELECT.

    Joins may appear directly on the select or nested in a parenthesized
    FROM expression; both are handled by walking join nodes.
    """
    aliases: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        if table.alias:
            aliases[table.alias] = name
        elif name not in aliases.values():
            # Unaliased base table maps to itself when no alias shadows it
            aliases[name] = name
    # Swap: prefer alias keys, keep bare table names as themselves too
    out = dict(aliases)
    for table in select.find_all(exp.Table):
        if table.alias:
            out.setdefault(table.name, table.name)
    return out


def _resolve(table: str, aliases: dict[str, str]) -> str:
    return aliases.get(table, table) if table else table


def _unresolved_default(aliases: dict[str, str], single_base: str | None) -> str:
    """Unqualified columns resolve to the single base table when unambiguous.

    ``SELECT amount FROM loan`` → amount belongs to loan; with multiple base
    tables an unqualified reference stays unknown ('').
    """
    return single_base or ""


def _source_columns(
    node: exp.Expression, aliases: dict[str, str],
) -> list[ColumnSource]:
    """All Column references inside ``node`` as source facts."""
    out: list[ColumnSource] = []
    bases = sorted({t for t in aliases.values() if t})
    single_base = bases[0] if len(bases) == 1 else None
    for col in node.find_all(exp.Column):
        table = col.table or ""
        base = _resolve(table, aliases) or _unresolved_default(aliases, single_base)
        out.append(ColumnSource(table=base, column=col.name, expr=col.sql().strip()))
    return out


def _output_columns(select: exp.Select, aliases: dict[str, str]) -> list[tuple[str, list[ColumnSource]]]:
    """Projection → (output_name, contributing source columns)."""
    out: list[tuple[str, list[ColumnSource]]] = []
    for proj in select.expressions:
        name = ""
        if isinstance(proj, exp.Alias):
            name = proj.alias
        elif isinstance(proj, exp.Column):
            name = proj.name
        else:
            name = proj.sql().split()[0].strip() if proj.sql().strip() else ""
        # Expression with no columns (constant) has no source lineage
        sources = [
            s for s in _source_columns(proj, aliases)
            if s.column != "unknown"
        ]
        if name:
            out.append((name, sources))
    return out


def _columns_read(select: exp.Select, aliases: dict[str, str]) -> list[tuple[str, str]]:
    """Deduped (table, column) pairs referenced anywhere in the SELECT."""
    pairs: set[tuple[str, str]] = set()
    bases = sorted({t for t in aliases.values() if t})
    single_base = bases[0] if len(bases) == 1 else None
    for node in select.find_all(exp.Column, exp.All):
        if isinstance(node, exp.All):
            # SELECT * — no column granularity, skip
            continue
        table = node.table or ""
        base = _resolve(table, aliases) or _unresolved_default(aliases, single_base)
        pairs.add((base, node.name))
    return sorted(pairs, key=lambda p: (p[0], p[1]))


def analyze_query(sql: str, dialect: str = "sqlite") -> QueryDigest | None:
    """Digest one SQL statement into lineage facts (None = unparseable)."""
    if not sql or not sql.strip():
        return None
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        # sqlglot raises on syntax errors / unsupported dialect
        return None

    kind = "query"
    name = ""
    inner: exp.Expression | None = parsed

    if isinstance(parsed, exp.Create):
        # SQLGlot exposes the creation kind ('VIEW'/'TABLE') as a plain
        # string on the Create node — views register as producers, plain
        # CREATE TABLE* (no SELECT body) stays an unparseable digest.
        kind = "create_view" if str(getattr(parsed, "kind", "")).upper() == "VIEW" else "create_table_as"
        name = parsed.this.name if parsed.this is not None else ""
        inner = parsed.expression
    elif isinstance(parsed, (exp.Insert, exp.Merge)):
        inner = parsed.expression

    select = inner if isinstance(inner, exp.Select) else None
    if select is None and isinstance(inner, exp.Expression):
        # One nested level: INSERT INTO ... SELECT, CTE-prefixed selects etc.
        select = next(iter(inner.find_all(exp.Select)), None)

    if select is None:
        # Not an actionable query: garbage statements, bare CTE, plain DDL
        # bodies (CREATE TABLE defs), VALUES-only INSERTs — no lineage facts.
        return None

    aliases = _base_aliases(select)
    tables = sorted({t for t in aliases.values() if t})
    return QueryDigest(
        kind=kind,
        name=name,
        tables_read=tables,
        columns_read=_columns_read(select, aliases),
        outputs=_output_columns(select, aliases),
    )


def normalization_key(sql: str) -> str:
    """Deterministic shard for deduping recorded queries.

    Whitespace/major-case normalized dialect-agnostic text; comments are
    stripped. Two SQL strings that differ only in formatting share a key.
    """
    if not sql:
        return ""
    import re

    cleaned = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.S)
    return " ".join(cleaned.split()).lower()