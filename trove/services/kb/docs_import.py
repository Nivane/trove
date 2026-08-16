"""Official documentation import — per-table files → KB column descriptions.

/kb init --docs <dir> 的输入目录:每个文件一张表(文件 stem = 表名)。
支持格式(列名容错,BIRD 官方 CSV 与通用格式都可用):
  - CSV:original_column_name|column_name|name +
         column_description|description|comment +
         value_description|enums|values
  - JSON/YAML:list of {"name", "description", "enums": [...]}

返回 {table_name: {column_name: {"description": str, "enums": [str]}}}。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml

_HEADER_ALIASES = {
    "name": ("original_column_name", "column_name", "name"),
    "description": ("column_description", "description", "comment", "desc"),
    "enums": ("value_description", "enums", "values", "value"),
}


def _resolve_headers(fieldnames: list[str]) -> dict[str, str | None]:
    """CSV 表头 → 规范键(小写去空格后匹配别名,未知表头忽略)。"""
    lowered = {h.strip().lower(): h for h in fieldnames}
    resolved: dict[str, str | None] = {}
    for key, aliases in _HEADER_ALIASES.items():
        resolved[key] = next((lowered[a] for a in aliases if a in lowered), None)
    return resolved


def _read_csv(path: Path) -> dict[str, dict[str, Any]]:
    cols: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return cols
        headers = _resolve_headers(reader.fieldnames)
        for row in reader:
            name = (row.get(headers["name"] or "", "") or "").strip()
            if not name:
                continue
            desc = (row.get(headers["description"] or "", "") or "").strip()
            enums_raw = (row.get(headers["enums"] or "", "") or "").strip()
            cols[name] = {"description": desc, "enums": [enums_raw] if enums_raw else []}
    return cols


def _read_records(path: Path) -> dict[str, dict[str, Any]]:
    """JSON/YAML:list of column records。"""
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cols: dict[str, dict[str, Any]] = {}
    if not isinstance(data, list):
        return cols
    for record in data:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name", "") or "").strip()
        if not name:
            continue
        enums = record.get("enums") or []
        cols[name] = {
            "description": str(record.get("description", "") or "").strip(),
            "enums": [str(e) for e in enums] if isinstance(enums, list) else [str(enums)],
        }
    return cols


def load_docs_tables(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """读取文档目录:每个文件一张表,文件 stem 为表名。"""
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    if not path.is_dir():
        return tables
    for file in sorted(path.iterdir()):
        if not file.is_file():
            continue
        suffix = file.suffix.lower()
        if suffix == ".csv":
            cols = _read_csv(file)
        elif suffix in (".json", ".yml", ".yaml"):
            cols = _read_records(file)
        else:
            continue
        if cols:
            tables[file.stem] = cols
    return tables


def apply_docs(
    tables: list[dict[str, Any]], docs: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """官方文档覆盖 LLM 草稿(docs 权威;未收录的表/列保持草稿)。"""
    if not docs:
        return tables
    for table in tables:
        doc = docs.get(table.get("name", ""), {})
        if not doc:
            continue
        for col in table.get("columns", []):
            entry = doc.get(col.get("name", ""))
            if not entry:
                continue
            if entry.get("description"):
                col["description"] = entry["description"]
            if entry.get("enums"):
                col["enums"] = list(entry["enums"])
    return tables
