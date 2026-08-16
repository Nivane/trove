"""Deterministic KB generation — terms/templates derived from schema + descriptions.

kb init 的 semantics/examples 部分从 LLM 起草改为确定性生成:
  - 每个表一条 COUNT 术语(别名 数量/记录数)
  - 每个有描述的非 ID 数值列:SUM/AVG 术语
  - 每个有描述的日期列:平均年份术语
  - 每个表 COUNT 模板 + 首条文本列的 GROUP BY 模板

无描述的列(如 A1~A16 这类不透明列名)不生成术语——名字无法可靠
推导,LLM 对此只会瞎猜(实证:financial KB 的 district 术语与官方
列描述系统性错位)。
"""

from __future__ import annotations

import re
from typing import Any

_TEXT_TYPES = ("char", "text", "varchar", "enum", "string", "character")
_NUMERIC_TYPES = (
    "int", "integer", "bigint", "smallint", "tinyint", "float",
    "double", "decimal", "numeric", "real",
)
_DATE_TYPES = ("date", "datetime", "timestamp", "time")

# 度量后缀:总量/平均插在这些词之前("贷款金额" → "贷款总金额")
_MEASURE_SUFFIXES = ("金额", "数量", "余额", "收入", "工资", "薪资", "期限", "笔数", "总额")

# 需要加引号的保留字表名
_RESERVED_NAMES = {"order", "group", "select", "from", "where", "table", "user", "key", "index"}


def business_label(description: str, table_name: str) -> str:
    """表描述 → 业务名词(描述首段去掉表后缀,拿不到就退回表名)。"""
    if not description:
        return table_name
    head = re.split(r"[，,、\s]", description.strip())[0]
    for suffix in ("信息表", "关系表", "记录表", "明细表", "统计表", "表"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
            break
    return head or table_name


def _is_id_column(name: str, description: str) -> bool:
    """ID 列不生成 SUM/AVG 术语(对 ID 求平均没有业务含义)。"""
    lowered = name.lower()
    return lowered.endswith(("_id", "id")) or "标识符" in description or "ID" in description


def _quote(name: str) -> str:
    return f'"{name}"' if name in _RESERVED_NAMES else name


def _insert_measure(word: str, description: str) -> str:
    """度量词前插入修饰语:贷款金额 → 贷款总金额;无度量后缀则前缀。

    括号注释("贷款期限（月数）")只属于描述,不参与术语命名。
    """
    name = re.sub(r"[（(][^）)]*[）)]\s*$", "", description)
    for suffix in _MEASURE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)] + word + suffix
    return word + name


def generate_terms(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从带描述的表格注释生成业务术语(确定性,无需 LLM)。"""
    terms: list[dict[str, Any]] = []
    for table in tables:
        name = table.get("name", "")
        label = business_label(str(table.get("description", "") or ""), name)

        terms.append({
            "term": f"{label}总数",
            "aliases": [f"{label}数量", f"{label}记录数"],
            "mapping": "COUNT(*)",
            "tables": [name],
            "definition": f"{label}表中的记录总数",
        })

        for col in table.get("columns", []):
            col_name = col.get("name", "")
            desc = str(col.get("description", "") or "").strip()
            col_type = str(col.get("type", "") or "").lower()
            if not desc or _is_id_column(col_name, desc):
                continue
            if any(m in col_type for m in _NUMERIC_TYPES):
                terms.append({
                    "term": _insert_measure("总", desc),
                    "aliases": [f"{desc}总和"],
                    "mapping": f"SUM({col_name})",
                    "tables": [name],
                    "definition": f"所有{desc}的总和",
                })
                terms.append({
                    "term": _insert_measure("平均", desc),
                    "aliases": [],
                    "mapping": f"AVG({col_name})",
                    "tables": [name],
                    "definition": f"所有{desc}的平均值",
                })
            elif any(m in col_type for m in _DATE_TYPES):
                terms.append({
                    "term": f"平均{desc}",
                    "aliases": [f"{desc}平均年份"],
                    "mapping": f"AVG(EXTRACT(YEAR FROM {col_name}))",
                    "tables": [name],
                    "definition": f"{desc}的平均年份",
                })
    return terms


def generate_templates(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个表生成 COUNT 模板 + 首条文本列的 GROUP BY 模板。"""
    templates: list[dict[str, Any]] = []
    for table in tables:
        name = table.get("name", "")
        label = business_label(str(table.get("description", "") or ""), name)
        quoted = _quote(name)

        templates.append({
            "template": True,
            "question": f"{label}表中有多少条记录？",
            "sql": f"SELECT COUNT(*) FROM {quoted}",
            "tags": [label, "行数", "聚合"],
        })

        for col in table.get("columns", []):
            col_type = str(col.get("type", "") or "").lower()
            if any(m in col_type for m in _TEXT_TYPES):
                col_name = col.get("name", "")
                desc = str(col.get("description", "") or "").strip() or col_name
                templates.append({
                    "template": True,
                    "question": f"按{desc}分组，统计每种{desc}的{label}数量",
                    "sql": f"SELECT {col_name}, COUNT(*) FROM {quoted} GROUP BY {col_name}",
                    "tags": [label, "分组", "聚合"],
                })
                break  # 每表一条 GROUP BY 模板(取首条文本列)
    return templates
