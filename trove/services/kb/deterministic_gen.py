"""Deterministic KB generation — terms/templates derived from schema + descriptions.

kb init 的 semantics/examples 部分从 LLM 起草改为确定性生成,默认英文
(评测用 benchmark 均为英文问题):
  - 每个表一条 COUNT 术语(别名 数量/记录数;en: {table} count)
  - 每个有描述的非 ID 数值列:SUM/AVG 术语
  - 每个有描述的日期列:平均年份术语
  - 每个表 COUNT 模板 + 首条文本列的 GROUP BY 模板

无描述的列(如 A1~A16 这类不透明列名)不生成术语——名字无法可靠
推导,LLM 对此只会瞎猜(实证:financial KB 的 district 术语与官方
列描述系统性错位)。en 模式下中文描述列同理跳过(无法确定性翻译)。
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

_CJK_RE = re.compile(r"[一-鿿]")


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
    """ID/标识类列不生成 SUM/AVG 术语(对 ID、账户号、银行代码求
    平均没有业务含义)。"""
    lowered = name.lower()
    return (
        lowered.endswith(("_id", "_to", "_from", "_code", "id"))
        or "标识符" in description or "ID" in description
    )


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


def generate_terms(
    tables: list[dict[str, Any]], lang: str = "en",
) -> list[dict[str, Any]]:
    """从带描述的表格注释生成业务术语(确定性,无需 LLM)。

    lang="en"(默认):表名/英文描述直接命名;中文描述列跳过(无法
    确定性翻译)。lang="zh":沿用中文命名规则。
    """
    terms: list[dict[str, Any]] = []
    for table in tables:
        name = table.get("name", "")
        label = name if lang == "en" else business_label(
            str(table.get("description", "") or ""), name)

        if lang == "en":
            terms.append({
                "term": f"number of {name} records",
                "aliases": [f"{name} count"],
                "mapping": "COUNT(*)",
                "tables": [name],
                "definition": f"total number of records in the {name} table",
            })
        else:
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
            if lang == "en" and _CJK_RE.search(desc):
                continue  # 中文描述无法确定性翻译成英文 → 视为无描述
            if any(m in col_type for m in _NUMERIC_TYPES):
                if lang == "en":
                    terms.append({
                        "term": f"total {desc}",
                        "aliases": [f"sum of {desc}"],
                        "mapping": f"SUM({col_name})",
                        "tables": [name],
                        "definition": f"sum of {desc} over all records",
                    })
                    terms.append({
                        "term": f"average {desc}",
                        "aliases": [f"avg {desc}"],
                        "mapping": f"AVG({col_name})",
                        "tables": [name],
                        "definition": f"average {desc} over all records",
                    })
                else:
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
                if lang == "en":
                    terms.append({
                        "term": f"average year of {desc}",
                        "aliases": [f"{desc} average year"],
                        "mapping": f"AVG(EXTRACT(YEAR FROM {col_name}))",
                        "tables": [name],
                        "definition": f"average year of {desc}",
                    })
                else:
                    terms.append({
                        "term": f"平均{desc}",
                        "aliases": [f"{desc}平均年份"],
                        "mapping": f"AVG(EXTRACT(YEAR FROM {col_name}))",
                        "tables": [name],
                        "definition": f"{desc}的平均年份",
                    })
    return terms


def _enum_values(enums: list[str], limit: int = 2) -> list[str]:
    """枚举条目 → 取值(前 limit 个,去重)。

    两种常见格式:
      'POPLATEK MESICNE=monthly issuance' → 取 = 前的值
      '"junior": junior class; "classic": ...' → 取引号内的值
    """
    values: list[str] = []
    for raw in enums or []:
        text = str(raw).strip()
        head = re.split(r"[=:]", text, maxsplit=1)[0].strip().strip("\"'")
        quoted = re.findall(r'"([^"]+)"', text)
        for candidate in quoted or [head]:
            candidate = candidate.strip()
            if candidate and candidate not in values:
                values.append(candidate)
        if len(values) >= limit:
            break
    return values[:limit]


def generate_templates(
    tables: list[dict[str, Any]], lang: str = "en",
) -> list[dict[str, Any]]:
    """确定性模板生成:COUNT + GROUP BY + 组合模板(JOIN 骨架/WHERE 过滤)。

    组合模板补齐单表原子模板缺的多表/过滤列覆盖(eval_retrieval 实测
    列覆盖仅 ~19%):同名 FK 列(account_id → account 表有同名列)推导
    JOIN;enum 列取样例值生成 WHERE 过滤模板。
    """
    templates: list[dict[str, Any]] = []
    for table in tables:
        name = table.get("name", "")
        label = name if lang == "en" else business_label(
            str(table.get("description", "") or ""), name)
        quoted = _quote(name)

        if lang == "en":
            templates.append({
                "template": True,
                "question": f"How many records are in the {name} table?",
                "sql": f"SELECT COUNT(*) FROM {quoted}",
                "tags": [name, "count", "aggregation"],
            })
        else:
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
                desc = str(col.get("description", "") or "").strip()
                if lang == "en" and (not desc or _CJK_RE.search(desc)):
                    desc = col_name  # 英文模式下中文描述退回到列名
                desc = desc or col_name
                if lang == "en":
                    templates.append({
                        "template": True,
                        "question": f"How many {name} records are there for each {desc}?",
                        "sql": f"SELECT {col_name}, COUNT(*) FROM {quoted} GROUP BY {col_name}",
                        "tags": [name, "group", "aggregation"],
                    })
                else:
                    templates.append({
                        "template": True,
                        "question": f"按{desc}分组，统计每种{desc}的{label}数量",
                        "sql": f"SELECT {col_name}, COUNT(*) FROM {quoted} GROUP BY {col_name}",
                        "tags": [label, "分组", "聚合"],
                    })
                break  # 每表一条 GROUP BY 模板(取首条文本列)

    # —— 组合模板:JOIN 骨架 + WHERE 过滤 ——
    # 单表原子模板对多表题的列覆盖不足(实测列覆盖 ~19%)。同名 FK
    # 列(fact.{dim}_id 且 dim 表有同名列)可确定性推导 JOIN 骨架;
    # enum 列取前 2 个样例值生成 WHERE 过滤模板。全部来自代码推导,
    # 非 gold 背诵,合规。
    by_name = {t.get("name", "").lower(): t for t in tables}

    def first_text_col(dim: dict) -> dict | None:
        for col in dim.get("columns", []):
            if any(m in str(col.get("type", "") or "").lower() for m in _TEXT_TYPES):
                return col
        return None

    def label(t: dict, fallback: str) -> str:
        if lang == "en":
            return fallback
        return business_label(str(t.get("description", "") or ""), fallback)

    for table in tables:
        tname = table.get("name", "")
        tquoted = _quote(tname)
        tlabel = label(table, tname)

        # A + B: {dim}_id FK → JOIN 骨架(同名键),维度表文本列可分组
        for col in table.get("columns", []):
            col_name = col.get("name", "")
            m = re.fullmatch(r"(.+)_id", col_name)
            if not m:
                continue
            dim = by_name.get(m.group(1))
            if not dim or dim.get("name") == tname:
                continue  # 排除主键({table}_id 不是 FK,避免自连接模板)
            if not any(c.get("name") == col_name for c in dim.get("columns", [])):
                continue
            dname = dim.get("name", "")
            dquoted = _quote(dname)
            dlabel = label(dim, dname)
            join_on = f"{tquoted}.{col_name} = {dquoted}.{col_name}"
            if lang == "en":
                templates.append({
                    "template": True,
                    "question": f"How many {tname} records are there in {dname}?",
                    "sql": f"SELECT COUNT(*) FROM {tquoted} JOIN {dquoted} ON {join_on}",
                    "tags": [tname, dname, "join", "aggregation"],
                })
            else:
                templates.append({
                    "template": True,
                    "question": f"{dlabel}中有多少{tlabel}记录？",
                    "sql": f"SELECT COUNT(*) FROM {tquoted} JOIN {dquoted} ON {join_on}",
                    "tags": [tlabel, dlabel, "连接", "聚合"],
                })
            dcol = first_text_col(dim)
            if dcol:
                dcol_name = dcol.get("name", "")
                ddesc = str(dcol.get("description", "") or "").strip()
                if lang == "en" and (not ddesc or _CJK_RE.search(ddesc)):
                    ddesc = dcol_name
                ddesc = ddesc or dcol_name
                group_col = f"{dquoted}.{dcol_name}"
                if lang == "en":
                    templates.append({
                        "template": True,
                        "question": f"How many {tname} records are there for each {ddesc} of {dname}?",
                        "sql": f"SELECT {group_col}, COUNT(*) FROM {tquoted} JOIN {dquoted} ON {join_on} GROUP BY {group_col}",
                        "tags": [tname, dname, "join", "group", "aggregation"],
                    })
                else:
                    templates.append({
                        "template": True,
                        "question": f"按{dlabel}的{ddesc}分组，统计每种{ddesc}的{tlabel}数量",
                        "sql": f"SELECT {group_col}, COUNT(*) FROM {tquoted} JOIN {dquoted} ON {join_on} GROUP BY {group_col}",
                        "tags": [tlabel, dlabel, "连接", "分组", "聚合"],
                    })

        # C: enum 列 WHERE 过滤模板
        for col in table.get("columns", []):
            vals = _enum_values(col.get("enums") or [])
            if not vals:
                continue
            col_name = col.get("name", "")
            desc = str(col.get("description", "") or "").strip()
            if lang == "en" and (not desc or _CJK_RE.search(desc)):
                desc = col_name
            desc = desc or col_name
            for val in vals:
                if lang == "en":
                    templates.append({
                        "template": True,
                        "question": f"How many {tname} records have {desc} = '{val}'?",
                        "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {col_name} = '{val}'",
                        "tags": [tname, col_name, "filter", "aggregation"],
                    })
                else:
                    templates.append({
                        "template": True,
                        "question": f"{tlabel}中{desc}为'{val}'的记录有多少？",
                        "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {col_name} = '{val}'",
                        "tags": [tlabel, col_name, "过滤", "聚合"],
                    })
    return templates
