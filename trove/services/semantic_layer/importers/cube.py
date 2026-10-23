"""Cube(编译后 meta JSON) → 中立 IR。

只接受 ``/cubejs-api/v1/meta`` 或 ``cubejs compile`` 的**编译产物**:
cube / dimension / measure / join 都已是展开后的 JSON。原始 JS/TS 模型
需要执行 JS 才能拿到真实定义,本模块明确不做。

``{CUBE}`` / ``{Other}`` 占位符在这里被消解:字段/指标表达式只保留
数据集作用域内的裸表达式;join SQL 把占位符替换成真实 cube 名后拆等值
键对。多对多 join、派生 cube、runningTotal 之类逐条进 ``unsupported``。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one

from trove.services.semantic_layer.importers.base import (
    ImportedDataset,
    ImportedField,
    ImportedMetric,
    ImportedRelationship,
    ImportedSemantic,
    SemanticImportError,
    aggregate_expression,
    dataset_refs,
    qualify_expression,
    unique_name,
    validate_expression,
)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_DATATYPE = {
    "string": "String",
    "number": "Decimal",
    "boolean": "Boolean",
    "time": "DateTime",
}

_MEASURE_AGG = {
    "count": "count",
    "countdistinct": "count_distinct",
    "count_distinct": "count_distinct",
    "sum": "sum",
    "avg": "avg",
    "min": "min",
    "max": "max",
}

_MEASURE_UNSUPPORTED = {
    "runningtotal": "runningTotal",
    "rank": "rank",
    "timeshift": "timeShift",
    "rollingwindow": "rollingWindow",
    "number": "number",
    "countdistinctapprox": "countDistinctApprox",
}


def parse_cube(path: str | Path) -> ImportedSemantic:
    """解析一份 Cube 编译 meta JSON(``{"cubes": [...]}`` 或裸 cube 列表)。"""
    p = Path(path)
    if not p.exists():
        raise SemanticImportError(f"路径不存在: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticImportError(f"Cube meta JSON 解析失败({p}): {exc}") from exc

    imported = ImportedSemantic(source="cube", input_path=str(p))
    cubes: list[dict[str, Any]] = []
    if isinstance(data, dict):
        raw = data.get("cubes")
        if isinstance(raw, list):
            cubes = [c for c in raw if isinstance(c, dict)]
        else:
            raise SemanticImportError(
                "Cube meta 缺少 cubes 列表 —— 需要 /cubejs-api/v1/meta 或 "
                "`cubejs compile` 的编译产物,不是原始 JS 模型")
        for view in data.get("views") or []:
            if isinstance(view, dict):
                vname = str(view.get("name") or "(unnamed)")
                imported.skip("cube_view_ignored", vname,
                              "Cube view 不映射为 Trove 数据集,已忽略")
    elif isinstance(data, list):
        cubes = [c for c in data if isinstance(c, dict)]
        imported.warn("bare_cube_list", str(p),
                      "输入是裸 cube 列表;已按单次 meta 处理")
    else:
        raise SemanticImportError("Cube meta 必须是 JSON 对象或 cube 列表")

    if not cubes:
        raise SemanticImportError(f"{p} 里没有 cube 定义")

    used_cubes: set[str] = set()
    for cube in cubes:
        name = str(cube.get("name") or "").strip()
        if not name:
            imported.warn("cube_unnamed", "", "cube 缺少 name,已跳过")
            continue
        if name in used_cubes:
            imported.warn("duplicate_cube", name, "同名 cube 重复出现,保留第一份")
            continue
        used_cubes.add(name)
        ds = _cube_dataset(cube, imported)
        if ds is not None:
            imported.datasets.append(ds)

    declared = {d.name for d in imported.datasets}
    used_rel: set[str] = set()
    used_metrics: set[str] = set()
    for cube in cubes:
        name = str(cube.get("name") or "").strip()
        if name not in declared:
            continue
        _cube_metrics(cube, declared, imported, used_metrics)
        _cube_joins(cube, declared, imported, used_rel)
    return imported


# ── 占位符 ──────────────────────────────────────────────────


def _strip_placeholders(sql: str, cube: str) -> str | None:
    """字段/指标表达式:{CUBE}. → 去掉,其余占位符 → 无法表达。"""
    text = str(sql or "").strip()
    if not text:
        return None
    text = re.sub(r"\{CUBE\}\s*\.\s*", "", text)
    text = text.replace("{CUBE}", "")
    if _PLACEHOLDER_RE.search(text):
        return None
    return text.strip()


def _fill_placeholders(sql: str, cube: str, other: str | None = None) -> str:
    text = str(sql or "")
    text = text.replace("{CUBE}", cube)
    if other is not None:
        text = text.replace(f"{{{other}}}", other)
    return text


# ── dataset / dimensions ────────────────────────────────────


def _cube_dataset(cube: dict[str, Any], imported: ImportedSemantic) -> ImportedDataset | None:
    name = str(cube.get("name") or "").strip()
    source = str(cube.get("sql_table") or "").strip()
    if not source:
        if cube.get("sql"):
            imported.skip("derived_cube_unsupported", name,
                          "cube 由 SQL 派生(sql 而非 sql_table),没有物理表可锚定,已跳过")
        else:
            imported.warn("cube_source_missing", name, "cube 缺少 sql_table,已跳过")
        return None
    source = source.replace('"', "").replace("`", "").replace("[", "").replace("]", "")
    ds = ImportedDataset(
        name=name,
        source=source,
        description=str(cube.get("description") or "").strip(),
    )
    seen: set[str] = set()
    for dim in cube.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        full = str(dim.get("name") or "").strip()
        local = full.rsplit(".", 1)[-1] if full else ""
        if not local or local in seen:
            continue
        sql = _strip_placeholders(dim.get("sql") or local, name)
        if sql is None:
            imported.warn("expression_unparsable", f"{name}.{local}",
                          "dimension 含跨 cube 占位符或空表达式,已跳过")
            continue
        if not sql:
            sql = local
        if not validate_expression(sql):
            imported.warn("expression_unparsable", f"{name}.{local}",
                          "dimension 表达式无法解析,已跳过")
            continue
        seen.add(local)
        ctype = str(dim.get("type") or "").strip().lower()
        datatype = _DATATYPE.get(ctype)
        if datatype is None and ctype:
            imported.warn("datatype_unknown", f"{name}.{local}",
                          f"Cube 类型 {ctype} 没有对应的 OSSIE datatype")
        ds.fields.append(ImportedField(
            name=local,
            expression=sql,
            datatype=datatype,
            description=str(dim.get("description") or dim.get("title") or "").strip(),
            is_time=ctype == "time",
        ))
    for segment in cube.get("segments") or []:
        if isinstance(segment, dict):
            sname = str(segment.get("name") or "(unnamed)")
            imported.skip("cube_segment_ignored", sname,
                          "Cube segment 不映射为 Trove 声明,已忽略")
    if any(isinstance(m, dict) and m.get("folders") for m in cube.get("measures") or []):
        imported.skip("cube_folders_ignored", name,
                      "Cube folders 属于 UI 分组,不写入 Trove 语义模型")
    return ds


# ── measures ────────────────────────────────────────────────


def _cube_metrics(
    cube: dict[str, Any],
    declared: set[str],
    imported: ImportedSemantic,
    used_metrics: set[str],
) -> None:
    name = str(cube.get("name") or "").strip()
    fields = [str(d.get("name") or "").rsplit(".", 1)[-1]
              for d in cube.get("dimensions") or [] if isinstance(d, dict)]
    primary = cube.get("primary_key") or cube.get("primaryKey")
    if isinstance(primary, list):
        primary = primary[0] if primary else None
    count_target = str(primary).rsplit(".", 1)[-1] if primary else (
        fields[0] if fields else None)

    for measure in cube.get("measures") or []:
        if not isinstance(measure, dict):
            continue
        full = str(measure.get("name") or "").strip()
        local = full.rsplit(".", 1)[-1] if full else ""
        target = f"{name}.{local or '?'}"
        if not local:
            imported.warn("measure_unnamed", name, "measure 缺少 name,已跳过")
            continue
        mtype = str(measure.get("type") or "").strip().lower()
        if mtype in _MEASURE_UNSUPPORTED:
            imported.skip("measure_type_unsupported", target,
                          f"Cube measure 类型 {_MEASURE_UNSUPPORTED[mtype]} 不可映射,已跳过")
            continue
        agg = _MEASURE_AGG.get(mtype)
        if agg is None:
            imported.skip("measure_type_unsupported", target,
                          f"Cube measure 类型 {mtype or '(空)'} 不可映射,已跳过")
            continue
        if measure.get("filters") or measure.get("segments"):
            imported.skip("measure_filter_unsupported", target,
                          "measure filters/segments 无法写入 Trove 指标载荷,已跳过")
            continue
        raw_sql = str(measure.get("sql") or "").strip()
        sql = _strip_placeholders(raw_sql, name) if raw_sql else ""
        if sql is None:
            imported.warn("expression_unparsable", target,
                          "measure 表达式含跨 cube 占位符,已跳过")
            continue
        if not sql:
            if agg not in ("count", "count_distinct") or not count_target:
                imported.skip("measure_expr_missing", target,
                              "measure 缺少 sql;只有 count 能回退到键列")
                continue
            sql = count_target
        qualified = qualify_expression(sql, name)
        if qualified is None:
            imported.warn("expression_unparsable", target,
                          "measure 表达式无法解析,已跳过")
            continue
        expression = aggregate_expression(agg, qualified)
        if expression is None:
            imported.skip("measure_type_unsupported", target,
                          f"Cube measure 类型 {mtype} 不可映射,已跳过")
            continue
        # Trove 指标名是全局的,Cube 成员名是 per-cube 的:Cube.count 与
        # Customers.count 在同一个模型里会撞名 —— 第二个起带数据集前缀。
        metric_name = local if local not in used_metrics else f"{name}_{local}"
        metric_name = unique_name(metric_name, used_metrics)
        imported.metrics.append(ImportedMetric(
            name=metric_name,
            expression=expression,
            datasets=dataset_refs(expression, declared),
            description=str(measure.get("description") or measure.get("title") or "").strip(),
        ))


# ── joins → relationships ───────────────────────────────────


def _cube_joins(
    cube: dict[str, Any],
    declared: set[str],
    imported: ImportedSemantic,
    used: set[str],
) -> None:
    name = str(cube.get("name") or "").strip()
    for join in cube.get("joins") or []:
        if not isinstance(join, dict):
            continue
        other = str(join.get("name") or "").strip()
        target = f"{name}->{other or '?'}"
        if not other:
            imported.warn("join_unnamed", name, "join 缺少 name,已跳过")
            continue
        relationship = str(join.get("relationship") or "").strip().lower()
        if relationship == "many_to_many":
            imported.skip("join_many_to_many_unsupported", target,
                          "多对多 join 需要 fan_out 豁免,而 fan_out 不能经草稿设置,"
                          "关系已跳过(手写进 semantics.yml)")
            continue
        if relationship not in ("many_to_one", "one_to_one", "one_to_many"):
            imported.skip("join_relationship_unsupported", target,
                          f"join 关系 {relationship or '(空)'} 不可映射,已跳过")
            continue
        if other not in declared:
            imported.warn("join_target_unknown", target,
                          f"join 目标 {other} 不是已导入的数据集(可能是派生 cube),已跳过")
            continue
        sql = _fill_placeholders(join.get("sql") or "", name, other)
        if "{" in sql or "}" in sql:
            imported.warn("join_expr_unparsable", target,
                          "join SQL 含未消解的占位符,已跳过")
            continue
        try:
            tree = parse_one(sql)
        except Exception:
            imported.warn("join_expr_unparsable", target,
                          "join SQL 无法解析为等值条件,已跳过")
            continue
        pairs = _equality_pairs(tree, name)
        if not pairs:
            imported.warn("join_expr_unparsable", target,
                          "join SQL 不是 cube 侧的等值键对,已跳过")
            continue
        from_cols = [p[0] for p in pairs]
        to_cols = [p[1] for p in pairs]
        if relationship == "one_to_many":
            from_ds, to_ds = other, name
            from_cols, to_cols = to_cols, from_cols
            cardinality = "1:N"
        else:
            from_ds, to_ds = name, other
            cardinality = "1:1" if relationship == "one_to_one" else "1:N"
        duplicate = any(
            r.from_dataset == from_ds and r.to_dataset == to_ds
            and r.from_columns == from_cols and r.to_columns == to_cols
            for r in imported.relationships
        )
        if duplicate:
            continue
        rel_name = unique_name(f"{from_ds}_to_{to_ds}", used)
        imported.relationships.append(ImportedRelationship(
            name=rel_name,
            from_dataset=from_ds,
            to_dataset=to_ds,
            from_columns=from_cols,
            to_columns=to_cols,
            cardinality=cardinality,
        ))


def _equality_pairs(tree: exp.Expression, cube: str) -> list[tuple[str, str]]:
    """AST → [(cube 列, 对方列)];任何非等值/非列比较 → 空列表。"""
    conditions: list[exp.Expression] = []

    def walk(node: exp.Expression) -> bool:
        if isinstance(node, exp.Paren):
            return walk(node.this)
        if isinstance(node, exp.And):
            return walk(node.this) and walk(node.expression)
        if isinstance(node, exp.EQ):
            conditions.append(node)
            return True
        return False

    if not walk(tree):
        return []
    pairs: list[tuple[str, str]] = []
    for cond in conditions:
        left = cond.this
        right = cond.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return []
        if left.table == cube and right.table != cube:
            pairs.append((left.name, right.name))
        elif right.table == cube and left.table != cube:
            pairs.append((right.name, left.name))
        else:
            return []
    return pairs
