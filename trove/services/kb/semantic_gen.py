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
  resulting semantics.yml is a drop-in superset of the old output;
- enums (P5.1): probed low-cardinality columns become
  `semantic_role: enum` with an identity `enum_display` ({code: code}),
  which the LLM annotation draft later enriches with human words — the
  deterministic half of "values live on the field, not in metrics".

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
_OSSIE_VERSION = "0.2.0.dev0"

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
    """一列 → 一个 field dict(标量表达式 + datatype + 语义角色)。"""
    entry: dict[str, Any] = {
        "name": col.name,
        "expression": {"dialects": [{"dialect": _ANSI, "expression": col.name}]},
    }
    dt = ossie_datatype(col.type)
    if dt:
        entry["datatype"] = dt
    role = infer_semantic_role(col.name, col.type, bool(getattr(col, "primary_key", False)))
    if role:
        entry["semantic_role"] = role
    return entry


def _source(table: TableInfo) -> str:
    """表 → physical source 引用(默认 schema 省略,其余带 schema 前缀)。"""
    if table.schema and table.schema not in ("main", ""):
        return f"{table.schema}.{table.name}"
    return table.name


def infer_semantic_role(name: str, sql_type: str | None, is_pk: bool) -> str:
    """确定性属性角色(零 LLM):identifier / time / measure / dimension。

    顺序即优先级:主键/id → identifier;日期 → time;非 id 数值 → measure;
    其余(含文本) → dimension。enum 角色需要真实取值(probe),生成期未知,
    留待声明层补充(有 enum_display 时消费端可标记)。
    """
    n = (name or "").lower()
    if is_pk or n == "id" or n.endswith("_id"):
        return "identifier"
    base = (sql_type or "").lower().split("(")[0].strip()
    if base in _DATE_TYPES | _TIME_TYPES | _DATETIME_TYPES | _TZ_TYPES:
        return "time"
    if base in _INT_TYPES | _FLOAT_TYPES | _DECIMAL_TYPES:
        return "measure"
    return "dimension"


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
                # 命名约定(``*_id`` → 主键)天然 many→one;显式声明基数,
                # 编译器对未声明基数的边保守 MISS(不再默认安全)。
                "cardinality": "1:N",
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


def _enum_display_from_values(values_text: str) -> dict[str, str]:
    """probe 的 ``"v1; v2"`` → 恒等 enum_display ``{v: v}``(值收编到字段)。

    恒等映射是确定性骨架:LLM 语义起草层把可读词补进 value 侧
    ({F: female}),code 键保持不变。空/超限值被跳过。
    """
    out: dict[str, str] = {}
    for v in (values_text or "").split(";"):
        v = v.strip()
        if v:
            out[v] = v
    return out


def generate_semantic_document(
    schema: SchemaInfo,
    model_name: str = "",
    terms: list[dict[str, Any]] | None = None,
    enums: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """schema → OSSIE semantic_model 文档 dict(结构层,确定性)。

    Args:
        schema: adapter 的物理 schema(TableInfo/ColumnInfo 列表)。
        model_name: semantic model 名(通常是 datasource)。
        terms: 可选 flat term 列表(确定性 generate_terms 输出),作为
            metrics 嵌进文档;缺省只产出结构骨架。
        enums: 可选 probe_enums 输出 ``{table: {column: "v1; v2"}}``;
            低基数列提升为 ``semantic_role: enum`` + 恒等 enum_display。
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
    _apply_enum_roles(datasets, enums or {})
    rels = relationships_from_schema(tables)

    model: dict[str, Any] = {"name": model_name, "datasets": datasets}
    if rels:
        model["relationships"] = rels
    metrics = _metrics_from_terms(terms)
    if metrics:
        model["metrics"] = metrics
    doc: dict[str, Any] = {"semantic_model": [model]}
    # OSSIE v0.2.0.dev0 文档级 version(透传;解析端容缺省)
    doc["version"] = _OSSIE_VERSION
    return doc


def _apply_enum_roles(
    datasets: list[dict[str, Any]],
    enums: dict[str, dict[str, str]],
) -> None:
    """probe 结果落位:低基数列 → semantic_role=enum + 恒等 enum_display。

    只作用于文本/维度列(取值已知),不动 identifier/measure/time 列。
    """
    by_name = {str(d.get("name")): d for d in datasets}
    for table_name, cols in enums.items():
        ds = by_name.get(table_name)
        if ds is None:
            continue
        field_by_name = {str(f.get("name")): f for f in ds.get("fields", [])}
        for col_name, values_text in cols.items():
            field = field_by_name.get(col_name)
            if field is None:
                continue
            display = _enum_display_from_values(values_text)
            if not display:
                continue
            # 仅提升原 dimension 列(identifier/measure/time 保持角色不动)
            if str(field.get("semantic_role") or "") in ("", "dimension"):
                field["semantic_role"] = "enum"
            field["enum_display"] = display