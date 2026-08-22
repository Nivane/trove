"""Deterministic structure-layer generation — datasets/fields/relationships.

`generate_semantic_document` converts a physical schema into the OSSIE
structural skeleton of a semantic model, **without any LLM**:

- datasets: name/source/primary_key + all columns as fields (datatype
  mapped from the SQL column type; temporal columns default to time
  dimensions via `dimension.is_time`);
- relationships: declared join graph inferred from `*_id` naming
  convention (the same convention as schema_linking._join_hints),
  many→one direction, deduplicated;
- metrics: carried over from the provided flat `terms` list (the
  deterministic COUNT/SUM/AVG terms kb init already generates) so the
  resulting semantics.yml is a drop-in superset of the old output.

This is 100% deterministic and regenerable from the schema, so it falls
inside the "kb init can generate it" rule — never hand-authored gold SQL.
The semantic layer (synonyms, business descriptions, opaque-column
mapping) is layered on top later by LLM drafting + human confirmation,
exactly like kb init / kb learn do for the rest of the KB.
"""
from __future__ import annotations

from typing import Any

from trove.core.types import SchemaInfo, TableInfo
from trove.services.kb.ossie_format import _metric_from_entry

_ANSI = "ANSI_SQL"

_TEXT_TYPES = frozenset({"char", "text", "varchar", "enum", "string",
                          "character", "clob", "varchar2", "nvarchar"})
_INT_TYPES = frozenset({"int", "integer", "bigint", "smallint", "tinyint",
                         "mediumint", "int2", "int4", "int8"})
_FLOAT_TYPES = frozenset({"float", "double", "real", "double precision"})
_DECIMAL_TYPES = frozenset({"decimal", "numeric"})
_DATE_TYPES = frozenset({"date"})
_TIME_TYPES = frozenset({"time"})
_DATETIME_TYPES = frozenset({"datetime", "timestamp"})
_TZ_TYPES = frozenset({"datetimetz", "timestamp with time zone"})

_DATATYPE_MAP: dict[str, str] = {
    **{t: "String" for t in _TEXT_TYPES},
    **{t: "Integer" for t in _INT_TYPES},
    **{t: "Float" for t in _FLOAT_TYPES},
    **{t: "Decimal" for t in _DECIMAL_TYPES},
    **{t: "Date" for t in _DATE_TYPES},
    **{t: "Time" for t in _TIME_TYPES},
    **{t: "DateTime" for t in _DATETIME_TYPES},
    **{t: "DateTimeTz" for t in _TZ_TYPES},
    "bool": "Boolean",
    "boolean": "Boolean",
}


def ossie_datatype(sql_type: str | None) -> str | None:
    """SQL 列类型 → OSSIE DataType(大小写不敏感,去掉精度括号)。"""
    base = (sql_type or "").lower().split("(")[0].strip()
    return _DATATYPE_MAP.get(base)


def _field_from_column(col: Any) -> dict[str, Any]:
    """一列 → 一个 field dict(标量表达式 + datatype)。"""
    entry: dict[str, Any] = {
        "name": col.name,
        "expression": {"dialects": [{"dialect": _ANSI, "expression": col.name}]},
    }
    dt = ossie_datatype(col.type)
    if dt:
        entry["datatype"] = dt
    return entry


def _source(table: TableInfo) -> str:
    """表 → physical source 引用(默认 schema 省略,其余带 schema 前缀)。"""
    if table.schema and table.schema not in ("main", ""):
        return f"{table.schema}.{table.name}"
    return table.name


def relationships_from_schema(
    tables: dict[str, TableInfo],
) -> list[dict[str, Any]]:
    """从 `*_id` 命名约定推导声明式 join 图(many→one 方向)。

    a.b_id → b (b 存在,命中 b.b_id 或 b.id)。与 schema_linking._join_hints
    同约定;方向取 many→one,与 OSSIE relationships 对齐。成对去重
    (同一对表只保留一条关系)。
    """
    rels: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()
    for tname, table in tables.items():
        target_cols = {c.name for c in table.columns}
        for col in table.columns:
            if not col.name.endswith("_id") or len(col.name) <= 3:
                continue
            target = col.name[:-3]
            if target == tname or target not in tables:
                continue
            other = {c.name for c in tables[target].columns}
            target_col = col.name if col.name in other else (
                "id" if "id" in other else None)
            if target_col is None:
                continue
            pair = frozenset((tname, target))
            if pair in seen:
                continue
            seen.add(pair)
            rels.append({
                "name": f"{tname}_to_{target}",
                "from": tname,
                "to": target,
                "from_columns": [col.name],
                "to_columns": [target_col],
            })
    return rels


def _metrics_from_terms(terms: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """flat term 列表(确定性 generate_terms 产物)→ OSSIE metric 列表。"""
    out: list[dict[str, Any]] = []
    for term in terms or []:
        mapping = str(term.get("mapping", "") or "").strip()
        if not mapping:
            continue
        out.append(_metric_from_entry(term))
    return out


def generate_semantic_document(
    schema: SchemaInfo,
    model_name: str = "",
    terms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """schema → OSSIE semantic_model 文档 dict(结构层,确定性)。

    Args:
        schema: adapter 的物理 schema(TableInfo/ColumnInfo 列表)。
        model_name: semantic model 名(通常是 datasource)。
        terms: 可选 flat term 列表(确定性 generate_terms 输出),作为
            metrics 嵌进文档;缺省只产出结构骨架。
    """
    tables = {t.name: t for t in schema.tables}
    datasets = [
        {
            "name": t.name,
            "source": _source(t),
            "primary_key": [c.name for c in t.columns if c.primary_key],
            "fields": [_field_from_column(c) for c in t.columns],
        }
        for t in schema.tables
    ]
    rels = relationships_from_schema(tables)

    model: dict[str, Any] = {"name": model_name, "datasets": datasets}
    if rels:
        model["relationships"] = rels
    metrics = _metrics_from_terms(terms)
    if metrics:
        model["metrics"] = metrics
    return {"semantic_model": [model]}