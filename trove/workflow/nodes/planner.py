"""Planner node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Planner failures are silent (empty plan): the
pipeline never blocks on planning.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.agent_loop import run_agent_loop
from trove.prompts import render
from trove.prompts.skills import render_skills
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)


def _parse_plan(response: str) -> dict[str, Any] | None:
    """结构化计划解析:摘掉可能的 markdown 围栏后按 JSON 解析。

    返回 None 表示模型没按格式输出(散文计划)——调用方原样回退,管线不中断。
    """
    text = (response or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _render_plan(data: dict[str, Any], lang: str = "en") -> str:
    """结构化计划 → 注入 gen_sql 提示词的文本(条件逐行,作用域显式)。"""
    zh = lang == "zh"
    lines: list[str] = []
    if data.get("tables"):
        lines.append(("表: " if zh else "Tables: ") + ", ".join(map(str, data["tables"])))
    if data.get("joins"):
        lines.append(("关联: " if zh else "Joins: ") + str(data["joins"]))
    conditions = data.get("conditions") or []
    if conditions:
        lines.append("条件:" if zh else "Conditions:")
        for c in conditions:
            # 模型偶发把条件输出成字符串数组(而非对象数组)——原样展示
            # 而不是崩溃丢整个 plan(崩溃 → 无计划 → SQL 质量下降 → RETRY 级联)
            if not isinstance(c, dict):
                lines.append(f"  - {c}")
                continue
            note = f"（{c['note']}）" if zh and c.get("note") else f" ({c['note']})" if c.get("note") else ""
            lines.append(f"  - {c.get('field')} {c.get('op')} {c.get('value')}{note}")
    if data.get("aggregation"):
        lines.append(("聚合: " if zh else "Aggregation: ") + str(data["aggregation"]))
    extreme = data.get("extreme")
    if isinstance(extreme, dict):
        scope = extreme.get("scope", "")
        lines.append(
            f"{('极值: ' if zh else 'Extreme: ')}{extreme.get('func')}({extreme.get('column')})"
            f" · scope: {scope}"
        )
    if data.get("ordering"):
        lines.append(("排序: " if zh else "Ordering: ") + str(data["ordering"]))
    if data.get("answer_columns"):
        lines.append(
            ("输出列: " if zh else "Answer columns: ") + ", ".join(map(str, data["answer_columns"]))
        )
    return "\n".join(lines)


def _plan_text(response: str, lang: str) -> str:
    """LLM 回复 → 计划文本:JSON 结构化渲染,解析失败回退散文原文。"""
    data = _parse_plan(response)
    return _render_plan(data, lang) if data is not None else (response or "").strip()


def validate_plan(
    plan: dict[str, Any] | None, schema: dict[str, set[str]] | None,
) -> list[str]:
    """校验计划引用的表/列真实存在(层1,确定性,零 LLM)。

    schema: 小写表名 → 小写列名集合(来自 connectors.get_schema())。
    表达式(含括号)、通配符 *、空字段跳过——只有直接列引用需要核实。
    返回错误列表(空 = 合法)。plan 或 schema 不可用 → 无法校验,返回空。
    """
    if not plan or not schema:
        return []
    errors: list[str] = []
    table_map = schema
    tables = [str(t) for t in (plan.get("tables") or [])]
    for t in tables:
        if t.lower() not in table_map:
            errors.append(f"table '{t}' not in schema")

    def check_field(field: Any, where: str) -> None:
        f = str(field or "").strip()
        if not f or f == "*" or "(" in f:
            return
        if "." in f:
            tbl, col = f.split(".", 1)
            if tbl.lower() not in table_map:
                errors.append(f"{where}: table '{tbl}' not in schema")
            elif col.lower() not in table_map[tbl.lower()]:
                errors.append(f"{where}: column '{col}' not in table '{tbl}'")
            return
        if not tables:
            errors.append(f"{where}: column '{f}' referenced but plan lists no tables")
        elif not any(
            f.lower() in table_map[t.lower()]
            for t in tables if t.lower() in table_map
        ):
            errors.append(f"{where}: column '{f}' not found in planned tables")

    for ac in plan.get("answer_columns") or []:
        check_field(ac, "answer_columns")
    for c in plan.get("conditions") or []:
        if isinstance(c, dict):
            check_field(c.get("field"), "conditions")
    return errors


def ensure_aggregate_answer_column(
    plan: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """分组聚合计划兜底:声明了聚合但 answer_columns 缺聚合指标列 → 补一列。

    分组计数/聚合类问题("每个地区的贷款用户数量"、"number of X per Y")
    的答案必须是两列:分组实体列 + 聚合指标列。planner 时常只把实体列写进
    answer_columns(聚合意图只表现在 aggregation 字段)——gen_sql 收到
    只有实体列的 answer_columns 就会只 SELECT 实体列、丢掉聚合结果。

    兜底:当 aggregation 非 none 且 answer_columns 没有任何含 `(` 的表达式
    列时,追加一个规范化的聚合指标列。函数名取 aggregation 字段的头部
    (count/sum/avg/min/max 等),列用 `*`(COUNT(*) 或 COUNT(1) 对任意
    基表都合法)——gen_sql 拿到"分组列 + count(*)"的权威指引后自然输出两列。
    返回修正后的 plan(新增 plan_field 标注),无改动时返回原 plan。
    """
    if not plan:
        return None
    agg = str(plan.get("aggregation") or "").strip().lower()
    if not agg or agg in ("none", "无"):
        return None
    cols = [str(a).strip() for a in (plan.get("answer_columns") or [])]
    if cols and any("(" in a for a in cols):
        return None  # 已有聚合表达式列 → 不重复补
    # aggregation 可能带修饰(如 "count(distinct x)")——取其函数名作占位前列
    func = re.split(r"[(\s]", agg, 1)[0] or "count"
    metric = f"{func}(*)"
    fixed = dict(plan)
    fixed["answer_columns"] = list(cols) + [metric]
    fixed["plan_field"] = "ensure_aggregate_answer_column"
    return fixed


# 语义级计数纠正的实体名词识别。收益率最高的是业务实体词 + 表名的双信号:
# 命中名词 → 找以其为表名(或含该名词)的表 → 取该表主键/ID 列做去重计数。
_ENTITY_COUNT_RE = re.compile(
    r"(?:用户|客户|顾客|人数|人员|多少(?:个|位|名)?(?:不同|不同|不同)?)"
    r"|\b(?:customers?|users?|clients?|persons?|people|holders?)\b",
    re.I,
)
_ENTITY_WORDS = {
    # 中文业务词 → 表名必须包含的 token(无 -> 匹配"近义表")
    "用户": ("client", "account", "customer"),
    "客户": ("client", "customer", "account"),
    "顾客": ("customer", "client"),
    "人员": ("client", "employee", "staff"),
    "人数": ("client", "customer", "employee"),
    # 英文 → 原词即表名或近义
    "customer": ("customer", "client"),
    "user": ("user", "client", "account"),
    "client": ("client", "customer"),
    "person": ("client", "customer", "user"),
    "people": ("client", "customer", "user"),
    "holder": ("account", "client", "card"),
}


def _is_entity_count_question(question: str, lang: str) -> bool:
    """问题是否在数"业务实体/人数"而不是"记录行数"。

    语词信号:「X 的用户数量/人数/多少用户」「number of X users/customers/
    people」。这类问题需要 COUNT(DISTINCT 实体),而 LLM planner 常把它
    误译成 COUNT(loan.loan_id) 之类的记录计数。纯正则,零 LLM。
    """
    q = question or ""
    if lang == "zh":
        return bool(re.search(
            r"(?:用户|客户|顾客|人员|人数|多少(?:名|位|个)?(?:用户|客户|顾客))",
            q,
        ))
    return bool(re.search(
        r"\b(?:customers?|users?|clients?|persons?|people|holders?)\b",
        q,
        re.I,
    ))


def _entity_tables(question: str, plan: dict | None, lang: str) -> list[str]:
    """从问题名词 + plan 的表单里选"实体表"(用于 COUNT(DISTINCT 实体.id))。

    策略:问题中出现的业务实体名词 → 在 plan.tables(及 schema 语境里)找表名
    包含该名词或近义的表;找到的候选优先 prefer 有一个 `_id` 结尾列且带
    FK 到所答指标表的表(schema 语境不可用时退化为选择顺序第一的候选)。
    返回小写表名列表(可能为空 = 无法确定实体表)。
    """
    q = (question or "").lower()
    tables = [str(t).lower() for t in ((plan or {}).get("tables") or [])]
    matches: list[str] = []
    for word, cousins in _ENTITY_WORDS.items():
        if word == "user" and not re.search(r"\bus(?:e|es)?\b", q, re.I):
            continue  # "user" 是词根,避免误吞 "custom user" 之类
        if re.search(re.escape(word), q) or any(re.search(re.escape(c), q) for c in cousins):
            for t in tables:
                if any(tok in t for tok in (word, *cousins)):
                    if t not in matches:
                        matches.append(t)
    # 无命名实体的近义命中时,回退:取 plan 表里带 *_id 主列候选(如 client/
    # account)——宁可猜实体表也不要让 planner 的 count(loan.loan_id) 溜过去。
    if not matches:
        for t in tables:
            if t not in matches and any(tok in t for tok in ("client", "account", "customer", "user")):
                matches.append(t)
    return matches


def _entity_id_column(table: str) -> str:
    """实体表选取去重计数列:优先 <表>_id;退化为 id。"""
    base = f"{table}_id"
    if table.endswith("ies"):
        base = f"{table[:-3]}y_id"
    elif table.endswith("s") and not table.endswith("ss"):
        base = f"{table[:-1]}_id"
    return base


def correct_entity_count_plan(
    plan: dict[str, Any] | None,
    question: str,
    lang: str = "en",
) -> dict[str, Any] | None:
    """语义级计数纠正:把"数用户/人数"计划的记录计数改写为去重实体计数。

    根因修复(第一轮就做对,不等 reflect 反推):plan 常把「X 的用户数量」写成
    ``count(loan.loan_id)``(数记录行),而问题语义要求 ``count(distinct
    实体.id)``(数去重用户)。规则 19 让 gen_sql 不敢反驳 plan——这里在
    plan→gen 之间用确定性规则把 plan 纠正好,gen 拿到对的就是对的。

    命中条件:
      1. aggregation 是 count(含 count distinct 之外的 count 族);
      2. 问题含实体计数语词(用户/客户/人数/customers/users/people...)。
    纠偏:
      - aggregation → ``count distinct <table>.<id>``;
      - answer_columns 里的记录计数表达式(count(<table>.<id>)) → 改去重版;
      - answer_columns 本身缺聚合列时追加去重计数列(交给 ensure_... 的性能
        由本函数先行,避免 count(*) 占位压过去重语义)。
    无法确定实体表 → None(不瞎猜,保持原 plan)。
    """
    if not plan:
        return None
    if not _is_entity_count_question(question, lang):
        return None
    agg = str(plan.get("aggregation") or "").strip().lower()
    if not agg or re.search(r"\bcount\s*\(", agg):
        if not agg or "count" not in agg:
            return None  # 非 count 聚合(sum/avg)或已显式去重 → 不动
    # 实体表解析(允许在 question 里,也可来自 plan.tables)
    tbls = _entity_tables(question, plan, lang)
    if not tbls:
        return None
    table = tbls[0]
    col = f"{table}.{_entity_id_column(table)}"
    expr = f"count(distinct {col})"

    cols = [str(a).strip() for a in (plan.get("answer_columns") or [])]
    replaced: list[str] = []
    for a in cols:
        low = a.lower()
        if re.match(r"^count\s*\(", low):
            replaced.append(expr)  # 记录计数(含别名/裸) → 去重版
        else:
            replaced.append(a)
    if not any("(" in a for a in replaced):
        replaced.append(expr)

    fixed = dict(plan)
    fixed["aggregation"] = f"count distinct {col}"
    fixed["answer_columns"] = replaced
    fixed["plan_field"] = "correct_entity_count_plan"
    return fixed


def answer_columns_mismatch(
    plan_json: dict[str, Any] | None, result_columns: list[str],
) -> list[str]:
    """plan 的 answer_columns 与执行结果列的一致性检查(层2,确定性)。

    仅当 answer_columns 里所有直接列引用都不在结果列中出现时才判定
    冲突——任一命中即放行(别名/表达式会让单列不一致成为常态噪音,
    全部缺失才是 SELECT 列表整体背离计划的强信号)。
    返回冲突描述列表(空 = 通过)。
    """
    if not plan_json:
        return []
    refs = [
        str(ac).strip() for ac in (plan_json.get("answer_columns") or [])
        if str(ac or "").strip() and str(ac).strip() not in ("*", "") and "(" not in str(ac)
    ]
    if not refs:
        return []
    lower_result = {str(c).lower() for c in result_columns}
    missing = [
        r for r in refs
        if r.lower() not in lower_result
        and r.split(".", 1)[-1].lower() not in lower_result
    ]
    if len(missing) < len(refs):
        return []
    return [
        f"answer_columns {refs} conflict with result columns {list(result_columns)}"
    ]


def _word_in_question(column: str, question_lower: str) -> bool:
    """列名(去表限定尾缀)是否以单词形式出现在问题文本中(单复数、下划线变体)。

    规则 19 允许的偏离:问题通顺地点名了某列(如 "districts")而 plan
    没写进 answer_columns 时,结果里带出该列不是错误——豁免之。
    """
    tail = column.split(".", 1)[-1].lower()
    for candidate in (tail, tail.replace("_", " ")):
        if re.search(rf"\b{re.escape(candidate)}s?\b", question_lower):
            return True
    return False


def _expr_signature(expr_text: str) -> tuple | None:
    """归一化一个表达式 answer 列 → (func, frozenset(列尾缀)) 签名。

    用于聚合表达式列的"按语义对账"而不是按名字符串:计划写
    ``count(loan.loan_id)``、SQL 投影成 ``COUNT(*) AS loan_count`` 时,
    两者签名都能归到 (count, 列集合),从而把 alias 与函数/列变化抹平。
    ``COUNT(*)`` 无列 → 空列集 = 通配(匹配同名聚合函数的任意列)。
    解析失败 → None(调用方回退位置配额)。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(expr_text)
    except Exception:
        return None
    funcs = list(tree.find_all(exp.AggFunc))
    if not funcs:
        return None
    f = funcs[0]
    name = f.sql().split("(", 1)[0].strip().lower()
    cols = frozenset(
        c.name.lower() for c in f.find_all(exp.Column) if c.name
    )
    return (name, cols)


def _sql_projections(sql: str) -> list[tuple[str, tuple | None]]:
    """解析 SQL 顶层 SELECT 投影 → [(结果名, 签名|None), ...]。

    结果名 = 别名(小写)或原始表达式文本;签名 = 该投影的聚合签名。
    解析失败/无 SELECT → []。仅供 extra 对账使用,绝不抛异常。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(sql)
    except Exception:
        return []
    select = tree.find(exp.Select)
    if select is None:
        return []
    out: list[tuple[str, tuple | None]] = []
    for proj in select.expressions:
        alias = None
        inner = proj
        if isinstance(proj, exp.Alias):
            alias = proj.alias
            inner = proj.this
        sig = _expr_signature(inner.sql())
        out.append((alias.lower() if alias else inner.sql().lower(), sig))
    return out


def extra_columns_mismatch(
    plan_json: dict[str, Any] | None,
    result_columns: list[str],
    question: str,
    sql: str | None = None,
) -> list[str]:
    """plan 的 answer_columns 与执行结果列的"多余列"检查(层2补充,确定性)。

    与 answer_columns_mismatch 互补:那个查"答案列全缺",这个查
    "结果列多余"。保守方向(宁漏勿误):
    - 前置条件:所有直接引用都出现在结果列中——任一缺失留给层2主检查,
      避免双重打回;
    - 豁免:结果列与 answer ref 大小写不敏感匹配(含去表限定尾缀);
      列名以单词形式出现在 question 文本中(规则 19 允许的偏离);
    - 聚合表达式 answer 列(含 `(`):优先用 sqlglot 按语义签名对账——
      聚合表达式在结果里以别名(COUNT(...) AS loan_count)或裸表达式
      呈现,无法按名字符串匹配,改按"函数名+列集"签名把对应结果列
      从多余列里划掉;SQL 不可解析时回退位置配额(旧语义)。
    剩余多余列 → 冲突。误伤成本 = 一次共享预算重试轮。
    """
    if not plan_json:
        return []
    answer_cols = [
        str(ac).strip() for ac in (plan_json.get("answer_columns") or [])
        if str(ac or "").strip() and str(ac).strip() not in ("*", "")
    ]
    refs = [a for a in answer_cols if "(" not in a]
    if not refs:
        return []
    lower_result = {str(c).lower() for c in result_columns}
    for r in refs:
        if r.lower() not in lower_result and r.split(".", 1)[-1].lower() not in lower_result:
            return []  # 有答案列缺失 → 交给层2主检查(宁漏勿误)
    ref_tails = {r.split(".", 1)[-1].lower() for r in refs}
    q_lower = (question or "").lower()
    extra = [
        c for c in result_columns
        if c.lower() not in ref_tails
        and not _word_in_question(c, q_lower)
    ]
    expr_cols = [a for a in answer_cols if "(" in a]
    # 聚合豁免的第二来源:plan 的 aggregation 字段。planner 常把聚合意图
    # 只写进 aggregation(= count/sum/avg...)而不写进 answer_columns,此时
    # SQL 顶层的聚合投影(COUNT(DISTINCT ...) AS num_loan_users)是预期输出,
    # 不能当多余列打回——COUNT(DISTINCT loan.account_id) 正是每区贷款用户数。
    plan_agg = str(plan_json.get("aggregation") or "").strip().lower()
    agg_declared = bool(plan_agg) and plan_agg not in ("none", "")

    # 聚合表达式对账:按签名把"聚合输出列"从多余列里划掉。
    # 1) 计划里每个聚合表达式一个签名;2) SQL 投影 → (结果名, 签名)。
    # 结果列名命中"与某计划聚合签名同函数的投影" → 该列是聚合输出,豁免。
    # plan 声明了聚合(aggregation 字段)时,所有聚合投影列都豁免(聚合列就
    # 是指标输出);不声明聚合时只豁免"与 answer 表达式签名匹配"的列。
    plan_sigs = [s for s in (_expr_signature(a) for a in expr_cols) if s is not None]
    projs = _sql_projections(sql) if sql else []
    claimed = set()
    if projs:
        if plan_sigs:
            claimed |= {
                name for name, sig in projs
                if sig is not None and any(_sig_compatible(sig, p) for p in plan_sigs)
            }
        if agg_declared:
            # 聚合投影 = 预期指标列(SQL 声明了聚合且 plan 也声明了聚合)
            claimed |= {name for name, sig in projs if sig is not None}
    extra = [c for c in extra if c.lower() not in claimed]

    if not extra:
        return []
    # 回退配额:无 SQL/无法解析时按"计划声明的聚合数"豁免前几列——宁漏勿误。
    if not claimed:
        quota = len(expr_cols) + (1 if agg_declared else 0)
        if len(extra) <= quota:
            return []
        extra = extra[quota:]
    if not extra:
        return []
    return [
        f"result columns {list(extra)} are not in the plan's answer_columns {refs} "
        "— output only the answer columns"
    ]


def _sig_compatible(a: tuple, b: tuple) -> bool:
    """两个聚合签名是否与对方兼容 → 该结果列是某计划聚合的输出。

    - 函数名必须相同;
    - 列集:任一侧为空(COUNT(*) 通配 / 计划用通配)即兼容;
      否则列集有交集才算同一目标列(表限定在签名里已去尾缀)。
    """
    if a[0] != b[0]:
        return False
    acols, bcols = a[1], b[1]
    if not acols or not bcols:
        return True
    return bool(acols & bcols)


async def _schema_map(connectors, datasource: str | None = None) -> dict[str, set[str]] | None:
    """真实 schema → 小写表名 → 小写列名集合;不可用 → None(跳过校验)。"""
    if connectors is None:
        return None
    try:
        schema = await connectors.get_schema(datasource)
        return {
            t.name.lower(): {c.name.lower() for c in t.columns}
            for t in schema.tables
        }
    except Exception:
        return None


def _short_value(v: Any) -> str:
    """观测里的单值:截断为短字符串。"""
    if v is None:
        return "null"
    s = str(v)
    return s[:40] + "…" if len(s) > 40 else s


async def _column_stats_text(
    connectors, table: str, column: str, datasource: str | None = None,
) -> str:
    """列画像观测:行数 / null 比例 / distinct / 样例 / 低基数高频值。

    运行时探测,**永不抛异常**——失败折叠成短错误文本。方言感知引号
    (schema_linking.py JOIN_PROBE 同款惯例):MySQL 反引号,其余双引号
    (SQLite 接受双引号标识符)。每个探测独立 5s 超时、失败静默跳过。
    高基数列(>30 distinct)不展示 top 值——top 只对低基数列有意义。
    """
    if connectors is None:
        return "error: no datasource available"
    try:
        adapter = await connectors.get(datasource)
        quote = "`" if adapter.dialect() == "mysql" else '"'
    except Exception as e:
        return f"error: {e}"
    t, c = str(table or "").strip(), str(column or "").strip()
    if not t or not c:
        return "error: both table and column are required"

    # 表/列存在性(schema 可用时;不可用则靠探查询的 SQL 错误兜底)
    distinct: int | None = None
    try:
        schema = await asyncio.wait_for(connectors.get_schema(datasource), timeout=5.0)
        tbl = next((x for x in schema.tables if x.name.lower() == t.lower()), None)
        if tbl is None:
            return f"table '{t}' not found"
        if c.lower() not in {col.name.lower() for col in tbl.columns}:
            return f"column '{c}' not found in table '{t}'"
    except Exception:
        pass

    q_t, q_c = f"{quote}{t}{quote}", f"{quote}{c}{quote}"

    agg: list | None = None
    try:
        r = await asyncio.wait_for(connectors.execute(
            f"SELECT COUNT(*), SUM({q_c} IS NULL), COUNT(DISTINCT {q_c}) FROM {q_t}",
            datasource,
        ), timeout=5.0)
        if r.rows and r.rows[0]:
            agg = r.rows[0]
            distinct = agg[2]
    except Exception:
        pass

    sample: list[str] = []
    try:
        r = await asyncio.wait_for(connectors.execute(
            f"SELECT DISTINCT {q_c} FROM {q_t} WHERE {q_c} IS NOT NULL LIMIT 5",
            datasource,
        ), timeout=5.0)
        sample = [_short_value(row[0]) for row in (r.rows or [])[:5]]
    except Exception:
        pass

    top: list[tuple[str, Any]] = []
    if distinct is None or 2 <= distinct <= 30:
        try:
            r = await asyncio.wait_for(connectors.execute(
                f"SELECT {q_c}, COUNT(*) FROM {q_t} GROUP BY {q_c} "
                f"ORDER BY COUNT(*) DESC, {q_c} LIMIT 10",
                datasource,
            ), timeout=5.0)
            top = [(_short_value(row[0]), row[1]) for row in (r.rows or [])[:10]]
        except Exception:
            pass

    parts: list[str] = []
    if agg is not None:
        rows, nulls = agg[0], agg[1] or 0
        null_ratio = round(nulls / rows, 3) if rows else 0.0
        parts.append(f"rows={rows} null_ratio={null_ratio} distinct={distinct}")
    if sample:
        parts.append("sample: " + ", ".join(sample))
    if top:
        parts.append("top: " + ", ".join(f"{v} ({n})" for v, n in top))
    if not parts:
        return f"error: no stats available for {t}.{c}"
    return "; ".join(parts)


def make_planner(
    llm: LLMGateway,
    config: AgentConfig,
    agentic: bool = True,
    connectors=None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the planner node bound to an LLM gateway."""

    async def planner(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}

        # 回退重跑：携带上一次失败与诊断，重定计划而不是重写原计划
        base_correction = " ".join(
            p for p in (state.error_feedback, state.error_analysis, state.reason) if p
        )
        schema_map = await _schema_map(connectors, state.datasource or None)
        # 计划起草走 fast 档(未配置 fast → 回退 target)
        model = config.model_fast or config.target or "openai/gpt-4o"
        system_prompt = render(
            "planner/system",
            lang=state.lang,
            has_tools=bool(agentic and connectors is not None),
        )
        # 方法论 skill:按节点确定性匹配(manifest.yml),注入 system prompt
        skill_block = render_skills("planner", lang=state.lang)
        if skill_block:
            system_prompt = f"{system_prompt}\n\n{skill_block}"
        llm_detail: dict[str, Any] | None = None
        trail = ""

        async def call_planner(correction: str) -> str:
            nonlocal llm_detail, trail
            prompt = render(
                "planner/user",
                lang=state.lang,
                question=state.question,
                schema_context=state.schema_context[:10000],
                evidence=state.evidence,
                time_context=state.time_context,
                history=state.history,
                correction=correction[:600] if correction else "",
                previous_plan=state.plan[:800] if state.plan else "",
            )
            if agentic and connectors is not None:
                from trove.llm.agent_loop import ToolRegistry

                registry = ToolRegistry(finish=True)

                async def table_columns(arguments: dict) -> str:
                    table = arguments.get("table", "")
                    schema = await connectors.get_schema(state.datasource or None)
                    for t in schema.tables:
                        if t.name.lower() == table.lower():
                            return ", ".join(f"{c.name} {c.type}" for c in t.columns)
                    return f"table '{table}' not found"

                async def column_stats(arguments: dict) -> str:
                    # 列画像:起草条件前锚定过滤值的真实取值与行数量级
                    return await _column_stats_text(
                        connectors,
                        arguments.get("table", ""),
                        arguments.get("column", ""),
                        state.datasource or None,
                    )

                registry.register(
                    "get_table_columns", table_columns,
                    description="Inspect the columns of one table.",
                    parameters={
                        "type": "object",
                        "properties": {"table": {"type": "string"}},
                        "required": ["table"],
                    },
                )
                registry.register(
                    "get_column_stats", column_stats,
                    description=(
                        "Inspect a column's real data: row count, null ratio, "
                        "distinct count, sample values, and (for low-cardinality "
                        "columns) the most frequent values with counts. "
                        "Use BEFORE drafting filter conditions to anchor values "
                        "to actual data — what type/frequency/status columns "
                        "really store, and the row-count scale of a table."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "table": {"type": "string"},
                            "column": {"type": "string"},
                        },
                        "required": ["table", "column"],
                    },
                )

                result = await run_agent_loop(
                    llm, model,
                    system=system_prompt,
                    user=prompt,
                    registry=registry,
                    tool_timeout_s=20.0,
                    time_budget_s=60.0,
                    max_rounds=3,
                    max_total_tokens=1500,
                    metadata={"node": "planner", "session_id": state.session_id, "run_id": state.run_id},
                )
                t = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                trail = t[:800]
                if not result.get("guard_hit"):
                    return result["content"]
                # 护栏降级:agent loop 原地打转/预算耗尽 → 退到直接生成
                # (plan 校验在 call_planner 之外,照常拦截幻觉列)
                logger.warning(
                    "Planner agent loop guard (%s, %d rounds); degrading to direct generation",
                    result.get("budget_why"), result["rounds"],
                )

            start = time.monotonic()
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                metadata={
                    "node": "planner",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            llm_detail = {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": (response or "").strip()[:200],
            }
            return response

        try:
            # 层1(plan 落地校验):引用的表/列必须真实存在;失败带修正
            # 自修正一次,仍失败则丢弃 plan(gen_sql 无 plan 照常生成,
            # 校验只拦截幻觉列,不让它变成 gen_sql 的钦点指令)
            raw = await call_planner(base_correction)
            plan_json = _parse_plan(raw)
            plan = _plan_text(raw, state.lang)
            errors = validate_plan(plan_json, schema_map)
            if errors:
                fix_correction = (
                    base_correction
                    + f" Your previous plan was invalid: {'; '.join(errors)}. "
                    + "Fix the plan so every table and column reference exists in the schema."
                )
                raw = await call_planner(fix_correction)
                plan_json = _parse_plan(raw)
                plan = _plan_text(raw, state.lang)
                errors = validate_plan(plan_json, schema_map)
            if errors:
                logger.info("Plan dropped after validation: %s", "; ".join(errors))
                update: dict[str, Any] = {
                    "plan": "",
                    "plan_json": None,
                    "plan_validation": {"status": "dropped", "errors": errors},
                }
                if llm_detail:
                    update["llm"] = llm_detail
                return update
            if not plan:
                return {}
            # 语义级计数纠正(优先):「X 的用户数量/人数」→ count(distinct 实体)。
            # planner 常把实体计数误译成 count(loan.loan_id) 的记录计数,这里
            # 在 plan→gen 之间确定性纠偏——gen 遵守规则 19 也不会做错。
            # 先于 ensure_aggregate_answer_column:后者只补 count(*) 占位列,
            # 若先跑会把已纠正的去重语义覆盖成 count(*) 通配。
            corrected = correct_entity_count_plan(plan_json, state.question, state.lang)
            if corrected is not None:
                plan_json = corrected
                plan = _render_plan(plan_json, state.lang)
            # 分组聚合兜底:声明了聚合但 answer_columns 缺聚合指标列 → 补列。
            # 修正后重渲染 plan 文本(gen_sql 以 answer_columns 为权威),
            # 并保留计划 JSON。此修正不违反 schema 校验(补的是表达式列)。
            fixed = ensure_aggregate_answer_column(plan_json)
            if fixed is not None:
                plan_json = fixed
                plan = _render_plan(plan_json, state.lang)
            update = {
                "plan": plan,
                "plan_json": plan_json,
                "plan_validation": {"status": "ok"},
            }
            if llm_detail:
                update["llm"] = llm_detail
            if trail:
                update["reasoning_history"] = [{"node": "planner", "text": trail}]
            return update
        except Exception as e:
            logger.warning("Planner failed (proceeding without a plan): %s", e)
            return {}

    return planner
