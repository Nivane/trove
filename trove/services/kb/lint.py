"""Static checks over parsed KB entries — catch silent KB regressions.

Covers the failure modes observed when the deterministic kb init replaced
a hand-crafted KB:
  - term mappings referencing unknown columns
  - SUM/AVG over ID/account-number columns (e.g. SUM(account_to))
  - example SQL that doesn't parse, references unknown tables, or writes
  - empty column descriptions (e.g. district A-columns left blank)
  - lessons with unusable patterns/notes
  - Chinese-only example questions (unreachable by English retrieval)

All functions are pure: parsed entries in, issue message list out.
"""

from __future__ import annotations

import re
from typing import Any

from sqlglot import ErrorLevel, exp, parse_one

from trove.services.kb.lesson_distill import MAX_PATTERN_LEN

_ID_LIKE_SUFFIXES = ("_id", "_to", "_from", "_code", "id")


def _parse(sql: str, dialect: str = "mysql") -> exp.Expression | None:
    """严格解析:语法错误返回 None(宽松模式下 "SELEC broken" 会被
    解析成 Alias 而非报错);调用方按需再判是否为查询语句。

    默认按 MySQL 解析(BIRD 评估目标);BIRD gold 的 DATE_FORMAT、
    CAST(x AS DOUBLE)、反引号限定列等写法通用方言会误报。
    """
    try:
        return parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return None


def lint_terms(
    terms: list[dict[str, Any]],
    schema: dict[str, set[str]],
    dialect: str = "mysql",
) -> list[str]:
    """Term mappings must reference existing columns; no SUM/AVG on ID-like columns.

    schema 的列名与 mapping 按小写比较(MySQL 列名大小写不敏感,
    schema_notes 里 A5 与 mapping 里 a5 是同一列)。
    """
    schema = {t: {c.lower() for c in cols} for t, cols in schema.items()}
    issues: list[str] = []
    for term in terms:
        name = str(term.get("term", ""))
        mapping = str(term.get("mapping", ""))
        tree = _parse(mapping, dialect)
        if tree is None:
            issues.append(f"术语「{name}」mapping 无法解析: {mapping[:60]}")
            continue
        agg = next(tree.find_all(exp.Sum, exp.Avg), None)
        bound = [t for t in term.get("tables", []) if t]
        for col in tree.find_all(exp.Column):
            col_name = (col.name or "").lower()
            col_table = (col.table or "").lower()
            if col_table:
                if col_table not in schema or col_name not in schema[col_table]:
                    issues.append(
                        f"术语「{name}」mapping 引用不存在的列 {col_table}.{col_name}")
            elif bound:
                if not any(col_name in schema.get(t, set()) for t in bound):
                    issues.append(f"术语「{name}」mapping 引用绑定表中不存在的列 {col_name}")
            elif not any(col_name in cols for cols in schema.values()):
                issues.append(f"术语「{name}」mapping 引用不存在的列 {col_name}")
            if agg is not None and col_name.endswith(_ID_LIKE_SUFFIXES):
                issues.append(f"术语「{name}」对 ID/账号类列 {col_name} 做聚合,无业务含义")
    return issues


def lint_examples(
    examples: list[dict[str, Any]],
    tables: set[str],
    dialect: str = "mysql",
) -> list[str]:
    """Example SQL must parse, read only known tables; questions need English reachability."""
    issues: list[str] = []
    for example in examples:
        question = str(example.get("question", ""))
        sql = str(example.get("sql", ""))
        tree = _parse(sql, dialect)
        if tree is None:
            issues.append(f"示例「{question[:30]}」SQL 无法解析: {sql[:60]}")
            continue
        if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Drop))
               for n in tree.walk()):
            issues.append(f"示例「{question[:30]}」包含写操作,不应作为参考示例")
            continue
        if not isinstance(tree, exp.Query):
            issues.append(f"示例「{question[:30]}」SQL 无法解析(或不是查询语句): {sql[:60]}")
            continue
        for table in tree.find_all(exp.Table):
            if table.name and table.name not in tables:
                issues.append(f"示例「{question[:30]}」引用了不存在的表 {table.name}")
        if question and not re.search(r"[A-Za-z]", question):
            issues.append(
                f"示例问题「{question[:30]}」无英文内容,英文问题检索命中率低(建议双语)")
    return issues


def lint_tables(tables: list[dict[str, Any]]) -> list[str]:
    """Columns with empty descriptions are knowledge blind spots.

    入参与 _parse_file 的 schema_notes payload 同形:columns 为
    {列名: 描述} 字典,表名在 "name" 字段。
    """
    issues: list[str] = []
    for table in tables:
        table_name = str(table.get("name", ""))
        for col_name, desc in (table.get("columns") or {}).items():
            if not str(desc or "").strip():
                issues.append(
                    f"表 {table_name} 的列 {col_name} 描述为空"
                    "(盲区,建议从官方文档导入或标注含义未知)")
    return issues


def lint_lessons(lessons: list[dict[str, Any]]) -> list[str]:
    """Lessons must carry a short, retrievable pattern and a non-empty note."""
    issues: list[str] = []
    for lesson in lessons:
        pattern = str(lesson.get("pattern", "") or "").strip()
        note = str(lesson.get("note", "") or "").strip()
        if len(pattern) > MAX_PATTERN_LEN:
            issues.append(
                f"lesson pattern 过长({len(pattern)}>{MAX_PATTERN_LEN}): {pattern[:40]}…")
        if not note:
            issues.append(f"lesson「{pattern[:30]}」note 为空")
    return issues
