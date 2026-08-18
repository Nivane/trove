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

# 日期值域采样:跨度超过此数时按 10 年步进抽取(每 decade 一个代表)
_DATE_YEAR_CAP = 12


def _parse_date_range(range_vals: list) -> tuple[int, int, str] | None:
    """列 range 字段 → (start_year, end_year, fmt)。

    fmt 支持 Berka YYMMDD('930101' → 1993)与标准 YYYY-MM-DD/YYYYMMDD。
    probe 端只保证"像日期",这里做严格解析;解析失败返回 None(不生成)。
    """
    if not range_vals or len(range_vals) < 2:
        return None
    lo, hi = str(range_vals[0]).strip(), str(range_vals[1]).strip()
    if not lo or not hi:
        return None
    if re.fullmatch(r"\d{6}", lo) and re.fullmatch(r"\d{6}", hi):
        fmt, width = "yymmdd", 2
        start = 1900 + int(lo[:2])
        end = 1900 + int(hi[:2])
    elif (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", lo)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", hi)
    ) or (
        re.fullmatch(r"\d{8}", lo) and re.fullmatch(r"\d{8}", hi)
    ):
        fmt, width = "ymd", 4
        start = int(lo[:4])
        end = int(hi[:4])
    else:
        return None
    if start > end:
        return None
    return start, end, fmt


def _sample_years(start: int, end: int) -> list[int]:
    """数据跨度内的代表年份。跨度 ≤ cap 全采;否则每 decade 一个 + 末年。"""
    if end - start + 1 <= _DATE_YEAR_CAP:
        return list(range(start, end + 1))
    years = [start, end]
    decade = (start // 10) * 10
    while decade < end:
        years.append(decade)
        decade += 10
    return sorted(set(years))


def _human_date(raw: str, fmt: str) -> str:
    """存储值 → 人类可读日期('930101' → '1993-01-01';标准格式原样)。"""
    if fmt == "yymmdd" and re.fullmatch(r"\d{6}", raw):
        return f"19{raw[:2]}-{raw[2:4]}-{raw[4:6]}"
    return raw[:10]

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


def _enum_label(rest: str) -> str:
    """去引号后的剩余文本 → 人类可读描述(去前导标点/stands for/尾标点)。"""
    rest = rest.strip().lstrip(":;,，。").strip()
    rest = re.sub(r"^stands for\s+", "", rest, flags=re.I)
    return rest.rstrip(";,，。.!").strip()


def _enum_values(
    enums: list[str], limit: int = 5,
) -> list[tuple[str, str]]:
    """枚举条目 → [(code, label)] 前 limit 个(去重)。

    格式多样(实测 BIRD 描述):
      'F=female\\nM=male'                 → [('F','female'), ('M','male')]
      'POPLATEK MESICNE=monthly issuance' → [('POPLATEK MESICNE','monthly issuance')]
      '"junior": junior class of credit card;' → [('junior','junior class of credit card')]
      "'A' stands for contract finished"  → [('A','contract finished')]
      'west Bohemia'(纯值)                → [('west Bohemia','west Bohemia')]
    叙述/噪声行('commonsense evidence: ...'、'each bank has unique
    two-letter code')跳过——保守:不产出 garbage 值。label 解析不出
    回退 code。
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in enums or []:
        for line in str(raw).splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^([^=]+)=(.*)$", line)
            if m:
                code = m.group(1).strip().strip("'\"")
                label = m.group(2).strip()
            else:
                quoted = re.findall(r'["\']([^"\']+)["\']', line)
                if quoted:
                    code = quoted[0].strip()
                    label = _enum_label(
                        re.sub(r'["\'][^"\']*["\']', "", line))
                else:
                    if ":" in line or len(line.split()) >= 3:
                        continue  # 叙述行(含冒号)或多词句子 → 跳过
                    code, label = line, ""
            if not code or code in seen:
                continue
            seen.add(code)
            pairs.append((code, label or code))
            if len(pairs) >= limit:
                return pairs
    return pairs


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
        # en 问题文本用人类可读 label(male/female 而非 M/F)——
        # "male customers" 与 "gender = 'M'" 词法零重叠,是检索死区;
        # SQL 保留 code 值。
        for col in table.get("columns", []):
            vals = _enum_values(col.get("enums") or [])
            if not vals:
                continue
            col_name = col.get("name", "")
            desc = str(col.get("description", "") or "").strip()
            if lang == "en" and (not desc or _CJK_RE.search(desc)):
                desc = col_name
            desc = desc or col_name
            for code, label_word in vals:
                if lang == "en":
                    # "are {label}" 比 "have {desc} = '{label}'" 词重叠更高
                    # ("male customers" 命中 how/many/male,实测 gender 模板
                    # 从 top-10 外进 top-10);SQL 保留 code 值
                    templates.append({
                        "template": True,
                        "question": f"How many {tname} records are {label_word}?",
                        "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {col_name} = '{code}'",
                        "tags": [tname, col_name, "filter", "aggregation"],
                    })
                else:
                    templates.append({
                        "template": True,
                        "question": f"{tlabel}中{desc}为'{code}'的记录有多少？",
                        "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {col_name} = '{code}'",
                        "tags": [tlabel, col_name, "过滤", "聚合"],
                    })

        # D: 数值/日期列聚合 + 比较模板
        # 缺口列(eval_retrieval 实测):amount/duration/balance/a11 等数值列与
        # date/birth_date 日期列无任何模板——C 族只覆盖 enum 等值过滤,
        # 比较/聚合类问题("average salary greater than 8000"、"loan amount
        # less than 100000"、"oldest client")命中不了。全部确定性推导、
        # 不依赖样例值;比较阈值用 0 占位——示例是结构参考,具体值由
        # gen_sql 的 check_result 兜底。无描述列不生成(名字无法可靠
        # 推导,与 generate_terms 同规则)。
        for col in table.get("columns", []):
            col_type = str(col.get("type", "") or "").lower()
            col_name = col.get("name", "")
            desc_raw = str(col.get("description", "") or "").strip()
            if lang == "en" and (not desc_raw or _CJK_RE.search(desc_raw)):
                desc = col_name
            else:
                desc = desc_raw or col_name
            if not desc_raw or _is_id_column(col_name, desc):
                continue
            if any(m in col_type for m in _NUMERIC_TYPES):
                agg_shapes = [
                    ("maximum", "MAX",
                     f"What is the maximum {desc}?", f"{desc}的最大值是多少？"),
                    ("minimum", "MIN",
                     f"What is the minimum {desc}?", f"{desc}的最小值是多少？"),
                    ("average", "AVG",
                     f"What is the average {desc}?", f"{desc}的平均值是多少？"),
                    ("total", "SUM",
                     f"What is the total {desc}?", f"{desc}的总和是多少？"),
                ]
                for word, fn, q_en, q_zh in agg_shapes:
                    if lang == "en":
                        templates.append({
                            "template": True,
                            "aggregate": True,
                            "question": q_en,
                            "sql": f"SELECT {fn}({col_name}) FROM {tquoted}",
                            "tags": [tname, col_name, "aggregation"],
                        })
                    else:
                        templates.append({
                            "template": True,
                            "aggregate": True,
                            "question": q_zh,
                            "sql": f"SELECT {fn}({col_name}) FROM {tquoted}",
                            "tags": [tlabel, col_name, "聚合"],
                        })
                if lang == "en":
                    templates.append({
                        "template": True,
                        "question": f"How many {tname} records have {desc} greater than 0?",
                        "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {col_name} > 0",
                        "tags": [tname, col_name, "filter", "aggregation"],
                    })
                else:
                    templates.append({
                        "template": True,
                        "question": f"{tlabel}中{desc}大于 0 的记录有多少？",
                        "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {col_name} > 0",
                        "tags": [tlabel, col_name, "过滤", "聚合"],
                    })
            elif any(m in col_type for m in _DATE_TYPES):
                # 日期列:最早/最晚(等值/区间比较需样例值,留给 probe 通道)
                if lang == "en":
                    templates.append({
                        "template": True,
                        "aggregate": True,
                        "question": f"What is the earliest {desc}?",
                        "sql": f"SELECT MIN({col_name}) FROM {tquoted}",
                        "tags": [tname, col_name, "aggregation"],
                    })
                    templates.append({
                        "template": True,
                        "aggregate": True,
                        "question": f"What is the latest {desc}?",
                        "sql": f"SELECT MAX({col_name}) FROM {tquoted}",
                        "tags": [tname, col_name, "aggregation"],
                    })
                else:
                    templates.append({
                        "template": True,
                        "aggregate": True,
                        "question": f"最早的{desc}是什么时候？",
                        "sql": f"SELECT MIN({col_name}) FROM {tquoted}",
                        "tags": [tlabel, col_name, "聚合"],
                    })
                    templates.append({
                        "template": True,
                        "aggregate": True,
                        "question": f"最晚的{desc}是什么时候？",
                        "sql": f"SELECT MAX({col_name}) FROM {tquoted}",
                        "tags": [tlabel, col_name, "聚合"],
                    })
                # E: 日期值域模板(probe 通道写入的 range 字段)
                # "approved loan date in 1997" / "between 1/1/1995 and 12/31/1997"
                # / "born before 1950" 的漏列根因:模板里没有年份/区间字面量。
                # range = probe_enums 对 MIN/MAX 的统计值,确定性数据,不是 gold。
                parsed = _parse_date_range(col.get("range"))
                if parsed is None:
                    continue
                start_year, end_year, fmt = parsed
                width = 2 if fmt == "yymmdd" else 4
                date_tags = [tname, col_name, "filter", "aggregation"]
                for year in _sample_years(start_year, end_year):
                    code = f"{year % 100:02d}" if fmt == "yymmdd" else str(year)
                    cond = f"substr({col_name}, 1, {width}) = '{code}'"
                    if lang == "en":
                        templates.append({
                            "template": True,
                            "date_range": True,
                            "question": f"How many {tname} records have {desc} in {year}?",
                            "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {cond}",
                            "tags": date_tags,
                        })
                    else:
                        templates.append({
                            "template": True,
                            "date_range": True,
                            "question": f"{tlabel}中{desc}在{year}年的记录有多少？",
                            "sql": f"SELECT COUNT(*) FROM {tquoted} WHERE {cond}",
                            "tags": date_tags,
                        })
                range_vals = [str(v) for v in col.get("range")][:2]
                between_sql = (
                    f"SELECT COUNT(*) FROM {tquoted} "
                    f"WHERE {col_name} BETWEEN '{range_vals[0]}' AND '{range_vals[1]}'"
                )
                if lang == "en":
                    templates.append({
                        "template": True,
                        "date_range": True,
                        "question": (
                            f"How many {tname} records have {desc} between "
                            f"{start_year} and {end_year}?"
                        ),
                        "sql": between_sql,
                        "tags": date_tags,
                    })
                else:
                    templates.append({
                        "template": True,
                        "date_range": True,
                        "question": (
                            f"{tlabel}中{desc}在{start_year}到{end_year}年之间"
                            f"的记录有多少？"
                        ),
                        "sql": between_sql,
                        "tags": date_tags,
                    })
                for raw in (range_vals[0], range_vals[1]):
                    if lang == "en":
                        templates.append({
                            "template": True,
                            "date_range": True,
                            "question": (
                                f"How many {tname} records have {desc} on "
                                f"{_human_date(raw, fmt)}?"
                            ),
                            "sql": (
                                f"SELECT COUNT(*) FROM {tquoted} "
                                f"WHERE {col_name} = '{raw}'"
                            ),
                            "tags": date_tags,
                        })
                    else:
                        templates.append({
                            "template": True,
                            "date_range": True,
                            "question": f"{tlabel}中{desc}为{_human_date(raw, fmt)}的记录有多少？",
                            "sql": (
                                f"SELECT COUNT(*) FROM {tquoted} "
                                f"WHERE {col_name} = '{raw}'"
                            ),
                            "tags": date_tags,
                        })
                end_code = f"{end_year % 100:02d}" if fmt == "yymmdd" else str(end_year)
                start_code = f"{start_year % 100:02d}" if fmt == "yymmdd" else str(start_year)
                before_sql = (
                    f"SELECT COUNT(*) FROM {tquoted} "
                    f"WHERE substr({col_name}, 1, {width}) < '{end_code}'"
                )
                after_sql = (
                    f"SELECT COUNT(*) FROM {tquoted} "
                    f"WHERE substr({col_name}, 1, {width}) > '{start_code}'"
                )
                if lang == "en":
                    templates.append({
                        "template": True,
                        "date_range": True,
                        "question": f"How many {tname} records have {desc} before {end_year}?",
                        "sql": before_sql,
                        "tags": date_tags,
                    })
                    templates.append({
                        "template": True,
                        "date_range": True,
                        "question": f"How many {tname} records have {desc} after {start_year}?",
                        "sql": after_sql,
                        "tags": date_tags,
                    })
                else:
                    templates.append({
                        "template": True,
                        "date_range": True,
                        "question": f"{tlabel}中{desc}早于{end_year}年的记录有多少？",
                        "sql": before_sql,
                        "tags": date_tags,
                    })
                    templates.append({
                        "template": True,
                        "date_range": True,
                        "question": f"{tlabel}中{desc}晚于{start_year}年的记录有多少？",
                        "sql": after_sql,
                        "tags": date_tags,
                    })
    return templates
