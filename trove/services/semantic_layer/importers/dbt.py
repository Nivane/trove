"""dbt Semantic Layer(YAML) → 中立 IR。

输入是 dbt 的 ``semantic_models`` + ``metrics`` YAML;可选的
``target/manifest.json`` 只用来把 ``ref('orders')`` / ``source('a','b')``
解析成物理表名。没有 manifest 也能导入:按 ref 名做表名并逐条警告
(``relation_unresolved``),不猜 schema。

明确的边界(不支持的构造逐条进 ``unsupported``,绝不静默吞):
- ``cumulative`` / ``conversion`` 指标、metric/measure ``filter``、
  percentile 之类聚合;
- 无法解析的表达式、残留 Jinja、歧义实体/度量;
- ``saved_queries`` / ``groups`` / ``time_spine`` 等外围段。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp, parse_one

from trove.services.kb.semantic_gen import ossie_datatype
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

_REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")
_SOURCE_RE = re.compile(
    r"source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)")
_JINJA_RE = re.compile(r"\{\{|\{%")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SKIP_DIRS = {"target", "dbt_packages", "node_modules", ".venv", "venv",
              ".git", "__pycache__", ".tox"}

_AGG_ALIASES = {
    "sum": "sum",
    "avg": "avg",
    "average": "avg",
    "min": "min",
    "max": "max",
    "count": "count",
    "count_distinct": "count_distinct",
}


def parse_dbt(
    path: str | Path,
    manifest_path: str | Path | None = None,
) -> ImportedSemantic:
    """解析一份 dbt 项目 / 单个 YAML。路径缺失或没有语义内容 → 抛错。"""
    base = Path(path)
    files = _yaml_files(base)
    manifest = _load_manifest(manifest_path)
    imported = ImportedSemantic(source="dbt", input_path=str(base))

    raw_models: list[dict[str, Any]] = []
    raw_metrics: list[dict[str, Any]] = []
    for file in files:
        data = _load_yaml(file)
        if not isinstance(data, dict):
            continue
        for key, bucket in (("semantic_models", raw_models), ("metrics", raw_metrics)):
            entries = data.get(key)
            if isinstance(entries, list):
                bucket.extend(e for e in entries if isinstance(e, dict))
        for key in ("saved_queries", "groups", "time_spine"):
            if data.get(key):
                imported.skip(
                    f"{key}_unsupported", key,
                    f"{key} 不属于 Trove 语义模型的可映射面,已忽略")

    if not raw_models and not raw_metrics:
        raise SemanticImportError(
            f"{base} 下没有找到 dbt semantic_models / metrics —— 需要一个 dbt "
            "Semantic Layer 项目的 YAML(或指向单个 YAML 文件)")

    used_models: set[str] = set()
    measure_index: dict[str, list[tuple[str, str]]] = {}
    entity_targets: dict[str, dict[str, tuple[str, int]]] = {}
    foreign: list[tuple[str, str, str]] = []  # (dataset, entity_name, column)

    for sm in raw_models:
        name = str(sm.get("name") or "").strip()
        if not name:
            imported.warn("semantic_model_unnamed", "", "semantic_model 缺少 name,已跳过")
            continue
        if name in used_models:
            imported.warn("duplicate_semantic_model", name,
                          "同名 semantic_model 重复出现,保留第一份")
            continue
        used_models.add(name)
        ds = _dataset_from_model(sm, imported, manifest)
        if ds is None:
            continue
        imported.datasets.append(ds)

        # 实体:主键 / 唯一键 / 外键(外键延后到所有模型就绪后解析目标)
        seen_entities: set[str] = set()
        for entity in sm.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            ename = str(entity.get("name") or "").strip()
            etype = str(entity.get("type") or "").strip().lower()
            if not ename or ename in seen_entities:
                continue
            seen_entities.add(ename)
            col = _column_name(entity.get("expr") or ename)
            if col is None:
                imported.warn(
                    "entity_expr_unsupported", f"{name}.{ename}",
                    "实体表达式不是列名,无法写入 primary_key/关系键对,已跳过")
                continue
            if etype in ("primary", "unique"):
                if etype == "primary":
                    if col not in ds.primary_key:
                        ds.primary_key.append(col)
                    priority = 0
                else:
                    ds.unique_keys.append([col])
                    priority = 1
                slot = entity_targets.setdefault(ename, {})
                old = slot.get(name)
                if old is None or priority < old[1]:
                    slot[name] = (col, priority)
            elif etype == "foreign":
                foreign.append((name, ename, col))
            else:
                imported.skip(
                    "entity_type_unsupported", f"{name}.{ename}",
                    f"实体类型 {etype or '(空)'} 不可映射")

        # 度量 → 指标表达式的原料(聚合式在这里定形,指标阶段只做引用)
        for measure in sm.get("measures") or []:
            if not isinstance(measure, dict):
                continue
            _index_measure(measure, ds, imported, measure_index)

    _resolve_relationships(imported, foreign, entity_targets)
    _resolve_metrics(imported, raw_metrics, measure_index)
    return imported


# ── 文件 / manifest ─────────────────────────────────────────


def _yaml_files(base: Path) -> list[Path]:
    if base.is_file():
        return [base]
    if not base.exists():
        raise SemanticImportError(f"路径不存在: {base}")
    files: list[Path] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".yml", ".yaml"):
            continue
        rel_parts = set(p.relative_to(base).parts)
        if rel_parts & _SKIP_DIRS:
            continue
        files.append(p)
    return files


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SemanticImportError(f"YAML 解析失败({path}): {exc}") from exc
    except OSError as exc:
        raise SemanticImportError(f"读取失败({path}): {exc}") from exc


def _load_manifest(manifest_path: str | Path | None) -> dict[str, Any] | None:
    if manifest_path is None:
        return None
    path = Path(manifest_path)
    if not path.exists():
        raise SemanticImportError(f"manifest 不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticImportError(f"manifest 解析失败({path}): {exc}") from exc
    return data if isinstance(data, dict) else None


def _manifest_nodes(manifest: dict[str, Any] | None) -> dict[str, list[str]]:
    """``model:<name>`` / ``source:<src>.<table>`` → 候选物理表名。"""
    index: dict[str, list[str]] = {}
    if not manifest:
        return index
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        rtype = str(node.get("resource_type") or "")
        name = str(node.get("name") or "")
        relation = str(node.get("relation_name") or node.get("alias") or "")
        if rtype == "model" and name:
            index.setdefault(f"model:{name}", []).append(_clean_relation(relation or name))
        elif rtype == "source" and name:
            src = str(node.get("source_name") or "")
            index.setdefault(f"source:{src}.{name}", []).append(
                _clean_relation(relation or name))
    for unique_id, node in (manifest.get("sources") or {}).items():
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "")
        src = str(node.get("source_name") or "")
        relation = str(node.get("relation_name") or node.get("identifier") or name)
        if name:
            index.setdefault(f"source:{src}.{name}", []).append(
                _clean_relation(relation or name))
    return index


def _clean_relation(text: str) -> str:
    return str(text or "").replace('"', "").replace("`", "").replace("[", "").replace("]", "")


def _resolve_relation(
    raw: Any,
    manifest: dict[str, Any] | None,
    imported: ImportedSemantic,
    target: str,
) -> str | None:
    text = str(raw or "").strip()
    if not text:
        imported.warn("model_missing", target, "semantic_model 缺少 model 引用,已跳过")
        return None
    if _JINJA_RE.search(text):
        imported.warn(
            "jinja_unresolved", target,
            "model 引用里残留 Jinja,无法确定物理表,已跳过(先 dbt compile)")
        return None
    index = _manifest_nodes(manifest)
    m = _REF_RE.search(text)
    if m:
        ref = m.group(1)
        hits = index.get(f"model:{ref}") or []
        if len(hits) == 1 and hits[0]:
            return hits[0]
        imported.warn(
            "relation_unresolved", target,
            f"ref('{ref}') 无法经 manifest 唯一解析,按表名 '{ref}' 导入")
        return ref
    m = _SOURCE_RE.search(text)
    if m:
        src, table = m.group(1), m.group(2)
        hits = index.get(f"source:{src}.{table}") or []
        if len(hits) == 1 and hits[0]:
            return hits[0]
        imported.warn(
            "relation_unresolved", target,
            f"source('{src}','{table}') 无法经 manifest 唯一解析,按表名 "
            f"'{table}' 导入")
        return table
    return _clean_relation(text)


def _column_name(raw: Any) -> str | None:
    text = str(raw or "").strip().strip('"`[]')
    return text if _COLUMN_RE.match(text) else None


# ── semantic_model → dataset ────────────────────────────────


def _dataset_from_model(
    sm: dict[str, Any],
    imported: ImportedSemantic,
    manifest: dict[str, Any] | None,
) -> ImportedDataset | None:
    name = str(sm.get("name") or "").strip()
    source = _resolve_relation(sm.get("model"), manifest, imported, name)
    if source is None:
        return None
    ds = ImportedDataset(
        name=name,
        source=source,
        description=str(sm.get("description") or "").strip(),
    )
    seen_fields: set[str] = set()
    for dim in sm.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        dname = str(dim.get("name") or "").strip()
        if not dname or dname in seen_fields:
            if dname:
                imported.warn("duplicate_dimension", f"{name}.{dname}",
                              "同名 dimension 重复出现,保留第一份")
            continue
        expr = str(dim.get("expr") or dname).strip()
        if _JINJA_RE.search(expr):
            imported.warn("jinja_unresolved", f"{name}.{dname}",
                          "dimension 表达式残留 Jinja,已跳过")
            continue
        if not validate_expression(expr):
            imported.warn("expression_unparsable", f"{name}.{dname}",
                          "dimension 表达式无法解析,已跳过")
            continue
        seen_fields.add(dname)
        datatype = ossie_datatype(dim.get("datatype")) if dim.get("datatype") else None
        ds.fields.append(ImportedField(
            name=dname,
            expression=expr,
            datatype=datatype,
            description=str(dim.get("description") or "").strip(),
            is_time=str(dim.get("type") or "").strip().lower() == "time",
        ))
    return ds


def _index_measure(
    measure: dict[str, Any],
    ds: ImportedDataset,
    imported: ImportedSemantic,
    index: dict[str, list[tuple[str, str]]],
) -> None:
    mname = str(measure.get("name") or "").strip()
    target = f"{ds.name}.{mname or '?'}"
    if not mname:
        imported.warn("measure_unnamed", ds.name, "measure 缺少 name,已跳过")
        return
    agg_raw = str(measure.get("agg") or measure.get("agg_type") or "").strip().lower()
    agg = _AGG_ALIASES.get(agg_raw)
    if agg is None:
        imported.skip("measure_agg_unsupported", target,
                      f"聚合方式 {agg_raw or '(空)'} 不可映射,已跳过")
        return
    if measure.get("filter") or measure.get("filters"):
        imported.skip("measure_filter_unsupported", target,
                      "measure filter 无法写入 Trove 指标载荷,已跳过")
        return
    expr = str(measure.get("expr") or mname).strip()
    if _JINJA_RE.search(expr):
        imported.warn("jinja_unresolved", target,
                      "measure 表达式残留 Jinja,已跳过")
        return
    if expr in ("*", ""):
        if agg != "count":
            imported.skip("measure_expr_missing", target,
                          "measure 缺少 expr,只有 count 能回退到键列")
            return
        fallback = next(iter(ds.primary_key), None)
        if fallback is None:
            fallback = next((f.name for f in ds.fields if not f.is_time), None)
        if fallback is None:
            imported.skip("measure_expr_missing", target,
                          "count measure 无 expr 且数据集没有可用列")
            return
        expr = fallback
    qualified = qualify_expression(expr, ds.name)
    if qualified is None:
        imported.warn("expression_unparsable", target,
                      "measure 表达式无法解析,已跳过")
        return
    expression = aggregate_expression(agg, qualified)
    if expression is None:
        imported.skip("measure_agg_unsupported", target,
                      f"聚合方式 {agg_raw} 不可映射,已跳过")
        return
    index.setdefault(mname, []).append((ds.name, expression))


# ── relationships ───────────────────────────────────────────


def _resolve_relationships(
    imported: ImportedSemantic,
    foreign: list[tuple[str, str, str]],
    targets: dict[str, dict[str, tuple[str, int]]],
) -> None:
    used: set[str] = set()
    for dataset, ename, column in foreign:
        slots = targets.get(ename) or {}
        candidates = [
            (ds, col) for ds, (col, _prio) in slots.items() if ds != dataset
        ]
        if not candidates:
            imported.warn(
                "entity_unresolved", f"{dataset}.{ename}",
                "外键实体找不到声明同名 primary/unique 实体的目标模型,关系已跳过")
            continue
        if len(candidates) > 1:
            names = ", ".join(sorted(ds for ds, _ in candidates))
            imported.warn(
                "entity_ambiguous", f"{dataset}.{ename}",
                f"实体 {ename} 命中多个目标模型({names}),关系已跳过")
            continue
        target_ds, target_col = candidates[0]
        rel_name = unique_name(f"{dataset}_to_{target_ds}", used)
        imported.relationships.append(ImportedRelationship(
            name=rel_name,
            from_dataset=dataset,
            to_dataset=target_ds,
            from_columns=[column],
            to_columns=[target_col],
            cardinality="1:N",
        ))


# ── metrics ─────────────────────────────────────────────────


class _Cycle(Exception):
    pass


def _resolve_metrics(
    imported: ImportedSemantic,
    raw_metrics: list[dict[str, Any]],
    measure_index: dict[str, list[tuple[str, str]]],
) -> None:
    defs: dict[str, dict[str, Any]] = {}
    for raw in raw_metrics:
        name = str(raw.get("name") or "").strip()
        if not name:
            imported.warn("metric_unnamed", "", "metric 缺少 name,已跳过")
            continue
        if name in defs:
            imported.warn("duplicate_metric", name, "同名 metric 重复出现,保留第一份")
            continue
        defs[name] = raw

    resolved: dict[str, tuple[str, list[str], str]] = {}
    declared = {d.name for d in imported.datasets}

    def resolve(name: str, stack: set[str]) -> tuple[str, list[str], str] | None:
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise _Cycle(name)
        raw = defs.get(name)
        if raw is None:
            return None
        stack = stack | {name}
        mtype = str(raw.get("type") or "simple").strip().lower()
        params = raw.get("type_params") or {}
        if not isinstance(params, dict):
            params = {}
        expr: str | None = None
        metric_type = ""
        datasets: list[str] = []
        if mtype == "simple":
            measure_name = str(params.get("measure") or "").strip()
            hits = measure_index.get(measure_name) or []
            if not hits and "__" in measure_name:
                hits = measure_index.get(measure_name.rsplit("__", 1)[-1]) or []
            if not hits:
                imported.warn("measure_unresolved", name,
                              f"simple 指标引用的 measure '{measure_name}' 不存在")
                return None
            if len(hits) > 1:
                names = ", ".join(sorted(ds for ds, _ in hits))
                imported.warn("measure_ambiguous", name,
                              f"measure '{measure_name}' 在多个模型里出现({names}),"
                              "无法唯一锚定")
                return None
            datasets = [hits[0][0]]
            expr = hits[0][1]
        elif mtype == "ratio":
            num = resolve(str(params.get("numerator") or ""), stack)
            den = resolve(str(params.get("denominator") or ""), stack)
            if num is None or den is None:
                imported.warn("metric_unresolved", name,
                              "ratio 指标的分子/分母解析失败,已跳过")
                return None
            expr = f"({num[0]}) / NULLIF({den[0]}, 0)"
            datasets = sorted(set(num[1]) | set(den[1]))
            metric_type = "ratio"
        elif mtype == "derived":
            raw_expr = str(params.get("expr") or raw.get("expr") or "").strip()
            substituted = _substitute_metrics(
                raw_expr, defs, resolve, stack, imported, name)
            if substituted is None:
                return None
            expr = substituted
            datasets = dataset_refs(expr, declared)
            metric_type = "derived"
        else:
            imported.skip("metric_type_unsupported", name,
                          f"指标类型 {mtype or '(空)'} 不可映射,已跳过")
            return None
        if raw.get("filter") or params.get("filter"):
            imported.skip("metric_filter_unsupported", name,
                          "metric filter 无法写入 Trove 指标载荷,已跳过")
            return None
        if not expr:
            imported.warn("metric_expr_missing", name, "指标表达式为空,已跳过")
            return None
        refs = dataset_refs(expr, declared)
        if not datasets:
            datasets = refs
        if not datasets:
            imported.warn("metric_unanchored", name,
                          "指标表达式没有引用任何已声明数据集,编译器无法锚定,已跳过")
            return None
        out = (expr, datasets, metric_type)
        resolved[name] = out
        return out

    for name, raw in defs.items():
        try:
            got = resolve(name, set())
        except _Cycle as exc:
            imported.warn("metric_cycle", name,
                          f"指标引用成环({exc}),已跳过")
            continue
        if got is None:
            continue
        expr, datasets, metric_type = got
        imported.metrics.append(ImportedMetric(
            name=name,
            expression=expr,
            datasets=list(datasets),
            description=str(raw.get("description") or "").strip(),
            metric_type=metric_type,
        ))


def _substitute_metrics(
    raw_expr: str,
    defs: dict[str, dict[str, Any]],
    resolve,
    stack: set[str],
    imported: ImportedSemantic,
    target: str,
) -> str | None:
    """derived 指标表达式里的指标名替换为各自的聚合式。"""
    if not raw_expr or _JINJA_RE.search(raw_expr):
        if _JINJA_RE.search(raw_expr or ""):
            imported.warn("jinja_unresolved", target,
                          "derived 表达式残留 Jinja,已跳过")
        else:
            imported.warn("metric_expr_missing", target, "derived 表达式为空,已跳过")
        return None
    try:
        tree = parse_one(raw_expr)
    except Exception:
        imported.warn("expression_unparsable", target,
                      "derived 表达式无法解析,已跳过")
        return None
    replaced = False
    for col in list(tree.find_all(exp.Column)):
        if col.table:
            continue
        dep = resolve(col.name, stack)
        if dep is None:
            if col.name in defs:
                imported.warn("metric_unresolved", target,
                              f"依赖指标 {col.name} 解析失败,已跳过")
            else:
                imported.warn("metric_unresolved", target,
                              f"表达式引用了未知指标 {col.name},已跳过")
            return None
        col.replace(exp.Paren(this=parse_one(dep[0])))
        replaced = True
    if not replaced:
        imported.warn("metric_unresolved", target,
                      "derived 表达式没有引用任何指标,无法锚定,已跳过")
        return None
    return tree.sql()
