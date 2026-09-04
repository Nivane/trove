"""Query-sketch node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Query-sketch failures are silent (empty plan): the
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
from trove.prompts import render
from trove.prompts.skills import render_skills
from trove.services.semantic_layer.plan import PlanQuery, parse_plan_query
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

# 时间粒度中文标签(渲染 zh plan 文本用;编译器消费原始 grain slug)
_GRAIN_ZH = {"year": "年", "quarter": "季度", "month": "月", "week": "周", "day": "日"}


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
    time_grain = data.get("time_grain")
    if isinstance(time_grain, dict) and time_grain.get("field"):
        grain = str(time_grain.get("grain") or "")
        grain_label = _GRAIN_ZH.get(grain, grain)
        lines.append(
            f"时间粒度: {time_grain.get('field')} 按{grain_label}"
            if zh else
            f"Time grain: {time_grain.get('field')} by {grain}"
        )
    having = data.get("having") or []
    if having:
        lines.append("聚合后过滤:" if zh else "Having:")
        for h in having:
            if not isinstance(h, dict):
                lines.append(f"  - {h}")
                continue
            target = h.get("metric") or h.get("field")
            lines.append(f"  - {target} {h.get('op')} {h.get('value')}")
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
    analysis = data.get("analysis")
    if isinstance(analysis, dict) and analysis.get("type"):
        parts_a = [str(analysis.get("type"))]
        if analysis.get("metric"):
            parts_a.append(f"metric={analysis.get('metric')}")
        if analysis.get("partition_by"):
            parts_a.append(f"partition_by={analysis.get('partition_by')}")
        if analysis.get("order_by"):
            parts_a.append(f"order_by={analysis.get('order_by')}")
        lines.append(("分析: " if zh else "Analysis: ") + " · ".join(parts_a))
    limit = data.get("limit")
    if limit:
        lines.append(("限量: " if zh else "Limit: ") + str(limit))
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
    的答案必须是两列:分组实体列 + 聚合指标列。query_sketch 时常只把实体列写进
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
    # 窗口分析(plan.analysis)自带权威度量列——确定性补列会引入多余投影
    # (多度量)导致分析 MISS,分析计划跳过这些旧形态兜底。
    if isinstance(plan.get("analysis"), dict):
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
    people」。这类问题需要 COUNT(DISTINCT 实体),而 LLM query_sketch 常把它
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
    # account)——宁可猜实体表也不要让 query_sketch 的 count(loan.loan_id) 溜过去。
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
    实体)``(数去重用户)。规则 19 让 gen_sql 不敢反驳 plan——这里在
    plan→gen 之间用确定性规则把 plan 纠正好,gen 拿到对的就是对的。

    两层决策:
      1. 优先精确层(answer_columns 含 count(<记录表>.<某列>)):从该表达式
         反推记录表 t,再到 plan.joins 里找形如 ``t.<fk> = <other>.<id>``
         的"记录表到实体的外键",改为 ``count(distinct t.<fk>)``。
         —— 例:count(loan.loan_id) + joins 含 ``loan.account_id=...``
            → count(distinct loan.account_id),正好是"贷款用户"的去重口径;
         2. 兜底层(无 count 表达式 / 外键不可定位):从问题名词 + plan.tables
         选实体表,但要验证该表出现在 joins 里(引用不存在的表会产生无效
         SQL),且不去碰需新增联表的实体。
    无法确定 → None(不瞎猜,保持原 plan)。
    """
    if not plan:
        return None
    # 分析计划走权威通道,不做记录→实体的去重计数改写(度量由 analysis 指定)。
    if isinstance(plan.get("analysis"), dict):
        return None
    if not _is_entity_count_question(question, lang):
        return None
    agg = str(plan.get("aggregation") or "").strip().lower()
    if "count" not in agg:
        return None
    if re.search(r"\bcount\s*\(\s*distinct", agg):
        return None  # 已是去重计数 → 无需纠正

    colses = [str(a).strip() for a in (plan.get("answer_columns") or [])]
    # answer_columns 里已含去重计数(count(distinct ...)) → 无需纠正
    if any(re.search(r"count\s*\(\s*distinct", a, re.I) for a in colses):
        return None
    joins = str(plan.get("joins") or "")

    expr = _distinct_expr_from_plan(colses, joins)
    if expr is None:
        expr = _distinct_expr_from_entities(question, plan, joins, lang)
    if expr is None:
        return None

    replaced: list[str] = []
    for a in colses:
        low = a.lower()
        if re.match(r"^count\s*\(", low):
            replaced.append(expr)  # 记录计数(含别名/裸) → 去重版
        else:
            replaced.append(a)
    if not any("(" in a for a in replaced):
        replaced.append(expr)

    fixed = dict(plan)
    fixed["aggregation"] = expr
    fixed["answer_columns"] = replaced
    fixed["plan_field"] = "correct_entity_count_plan"
    return fixed


def _distinct_expr_from_plan(cols: list[str], joins: str) -> str | None:
    """精确层:从 answer_columns 的 count(记录表.列) + joins 外键推去重实体列。

    只认 ``count(<t>.<anything>)`` 且 joins 里有 ``<t>.<fk> = ... <id>``:
    把记录计数(count 行)改成 count(distinct 记录表.外键列)。外键列名通常
    即"实体归属",如 count(loan.loan_id) → count(distinct loan.account_id)。
    """
    for a in cols:
        m = re.match(r"^count\s*\(\s*([A-Za-z_][\w]*)\.(\w+)\s*\)", a, re.I)
        if not m:
            continue
        tbl, col = m.group(1), m.group(2)
        if col.lower() == f"{tbl}_id".lower():
            continue  # count(loan.loan_id) 是记录主键,不是外键;继续找外键
        # joins 里该表的其它列作为 <=> 键(通常是外键,如 account_id)
        fk = re.search(
            rf"\b{re.escape(tbl)}\s*\.\s*(\w+)\s*=\s*[A-Za-z_][\w]*\s*\.\s*(\w+)",
            joins, re.I,
        )
        if fk:
            return f"count(distinct {tbl}.{fk.group(1)})"
    # 记录主键在 count 里,退一层:从 joins 找记录表级联的外键
    for a in cols:
        m = re.match(r"^count\s*\(\s*([A-Za-z_][\w]*)\.\w+\s*\)", a, re.I)
        if not m:
            continue
        tbl = m.group(1)
        fk = re.search(
            rf"\b{re.escape(tbl)}\s*\.\s*(\w+)\s*=\s*[A-Za-z_][\w]*\s*\.\s*(\w+)",
            joins, re.I,
        )
        if fk:
            return f"count(distinct {tbl}.{fk.group(1)})"
    return None


def _distinct_expr_from_entities(
    question: str, plan: dict, joins: str, lang: str,
) -> str | None:
    """兜底层:从问题名词挑实体表(必须已出现在 joins 里,避免引不存在的表)。"""
    tables = [str(t).lower() for t in ((plan or {}).get("tables") or [])]
    for t in _entity_tables(question, plan, lang):
        if re.search(rf"\b{re.escape(t)}\s*\.", joins, re.I) or t in tables:
            return f"count(distinct {t}.{_entity_id_column(t)})"
    return None


def _answer_ref_in_results(ref: str, lower_result: set[str],
                           result_lower: list[str]) -> bool:
    """answer 列引用是否出现在结果列中。

    先做精确匹配(整列 / 去表限定尾缀);再识别**表达式投影**——时间分桶
    等把 ``loan.date`` 渲染成 ``DATE_FORMAT(loan.date, '%Y')`` 时,完整
    表限定引用会以子串形式出现在结果列里。未限定列名则按词边界匹配
    尾缀,避免 ``update_date`` 误吞 ``date``。其余情形维持旧精确语义。
    """
    rl = ref.lower()
    if rl in lower_result:
        return True
    tail = rl.split(".", 1)[-1]
    if tail in lower_result:
        return True
    qualified = "." in rl
    for c in result_lower:
        # 完整表限定引用(loan.date)以子串出现在结果列表达式里 →
        # 时间分桶等变换;未限定列名只做词边界尾缀匹配,防 date ⊂ update_date。
        if qualified and rl in c:
            return True
        if "." not in rl and re.search(rf"\b{re.escape(tail)}\b", c):
            return True
    return False


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
    result_lower = [str(c).lower() for c in result_columns]
    missing = [
        r for r in refs
        if not _answer_ref_in_results(r, lower_result, result_lower)
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
    # 聚合豁免的第二来源:plan 的 aggregation 字段。query_sketch 常把聚合意图
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
    """真实 schema → 小写表名 → 小写列名集合;不可用 → None(跳过校验)。

    Phase B(决策 1):仅用于计划引用存在性校验(层1),不注入 LLM 上下文,
    也不作为 agent 的探测通道。
    """
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


def _plan_has_intent(plan_json: dict[str, Any] | None) -> bool:
    """计划是否表达真实查询意图(可解析且含可编译成分)。

    语义优先(Phase A)的 refuse 触发前提:plan 可解析且含 aggregation/
    answer_columns/conditions 之一——否则视为空洞/退化计划,不拒绝,
    交由 gen_sql 从 semantic_context 生成。
    """
    if not plan_json:
        return False
    return bool(
        plan_json.get("aggregation")
        or plan_json.get("answer_columns")
        or plan_json.get("conditions")
    )


_TIME_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})$")


def _inject_time_condition(
    plan: dict[str, Any] | None,
    time_context: str,
    model,
    matched: list[str],
) -> dict[str, Any] | None:
    """解析出的时间范围 → 确定性注入时间维度的区间条件。

    P1-4:parse_date 的产物不再只是 prompt 文本——把它落成 plan 条件,
    覆盖内问题编译 SQL 必然带时间过滤,未覆盖问题 gen_sql 的 plan 文本
    也带该条件。时间字段选择:plan 命中的 metric 若声明 ``agg_time_dimension``
    则优先用它(即使 matched 内多时间字段也能判定);否则要求 matched 内
    **唯一**时间字段。无法判定(无时间字段 / 范围格式非法 / 已有该字段
    条件)→ None,不猜,保持原 plan。
    """
    m = _TIME_RANGE_RE.match((time_context or "").strip())
    if not m or not plan:
        return None
    if not _plan_has_intent(plan):
        return None  # 退化/空洞计划不注入(避免凭空造出拒绝前提)
    from trove.services.semantic_layer.compiler import resolve_time_field

    preferred = _plan_metric_time_dimension(plan, model)
    resolved = resolve_time_field(model, list(matched), preferred=preferred)
    if resolved is None:
        return None
    ds, field = resolved
    ref = f"{ds}.{field.name}"
    conds = list(plan.get("conditions") or [])
    if any(
        isinstance(c, dict) and str(c.get("field", "")).lower() == ref.lower()
        for c in conds
    ):
        return None  # 已有该字段条件,不重复注入
    start, end = m.group(1), m.group(2)
    fixed = dict(plan)
    fixed["conditions"] = [
        {"field": ref, "op": ">=", "value": start, "note": "resolved time range start"},
        {"field": ref, "op": "<=", "value": end, "note": "resolved time range end"},
    ] + conds
    fixed["plan_field"] = "inject_time_condition"
    return fixed


def _plan_metric_time_dimension(plan: dict[str, Any] | None, model) -> str | None:
    """plan 命中的首个 metric 的时间维度锚点(agg_time_dimension 或数据集内唯一时间字段)。

    优先 metric 显式声明的 ``agg_time_dimension``;未声明时回退到该 metric
    锚定数据集(**非整个 matched 集**)内的唯一时间字段——多表匹配场景下
    matched 常有多个时间字段,``resolve_time_field`` 因不唯一无法判定,
    导致时间条件注入失败、覆盖内年份题漏过滤。聚合表达式候选(含 ``(``)
    按聚合签名匹配 metric,不再跳过。
    """
    if not plan or model is None:
        return None
    candidates = [str(plan.get("aggregation") or "").strip()]
    candidates += [str(ac).strip() for ac in (plan.get("answer_columns") or [])]
    from trove.services.semantic_layer.compiler import (
        _agg_signature,
        _is_time_field,
        _sig_compatible,
    )

    for cand in candidates:
        if not cand:
            continue
        sig = _agg_signature(cand) if "(" in cand else None
        for m in model.metrics:
            name_match = (
                "(" not in cand and m.name.strip().lower() == cand.lower()
            )
            m_sig = _agg_signature(m.expression) if sig is not None else None
            sig_match = (
                sig is not None
                and m_sig is not None
                and _sig_compatible(sig, m_sig)
            )
            if not (name_match or sig_match):
                continue
            if m.agg_time_dimension:
                return m.agg_time_dimension
            # 回退:metric 锚定数据集内唯一时间字段(优先于 matched 全局唯一)
            if m.datasets:
                ds_names = [str(d).lower() for d in m.datasets]
                fields: list[tuple[str, Any]] = []
                for d in model.datasets:
                    if d.name.lower() not in ds_names:
                        continue
                    for f in d.fields:
                        if _is_time_field(f):
                            fields.append((d.name, f))
                if len(fields) == 1:
                    return f"{fields[0][0]}.{fields[0][1].name}"
    return None


def _compile_semantic(
    plan: dict[str, Any] | PlanQuery | None,
    matched: list[str],
    semantic_layer,
    dialect: str = "sqlite",
) -> tuple[tuple[str, str] | None, CompileMiss | None]:
    """语义层覆盖内 → ((权威 SQL, 提示块), None);MISS → (None, CompileMiss)。

    受限选择编译:plan 的 metric/group_by/filters 必须全部解析到已声明
    metric/field/relationship,AND guardrail 放行才注入 gen_sql;否则原样
    走现有通道。全程确定性,零额外 LLM 调用。miss reason(结构化分因)
    带出供 query_sketch/refuse/eval 归因——不再被丢弃成笼统「uncovered」。

    入参可为强类型 PlanQuery(query_sketch 解析后的 AST)或 raw dict——
    编译器内部统一按 dict 流处理,两条路径产物字节级一致。dialect 来自
    state(适配器),驱动时间分桶等方言感知渲染。
    """
    from trove.services.semantic_layer.compiler import (
        CompileMiss,
        CompileResult,
        SemanticCompiler,
        validate_compiled_sql,
    )

    if isinstance(plan, PlanQuery):
        plan = plan.to_dict()
    if plan is None or not matched or semantic_layer is None:
        return None, CompileMiss("no_plan_or_matched", "")
    try:
        model = semantic_layer.model()
        if model is None:
            return None, CompileMiss("no_plan_or_matched", "no semantic model")

        result = SemanticCompiler(model).compile_detailed(
            plan, list(matched), force_dialect=dialect)
        if isinstance(result, CompileMiss):
            return None, result
        violations = validate_compiled_sql(result.sql, model, list(matched))
        if violations:
            logger.info(
                "Compiled SQL rejected by guardrail: %s", "; ".join(violations))
            return None, CompileMiss(
                "guardrail_rejected", "; ".join(violations))
        return (result.sql, result.block), None
    except Exception as e:
        logger.warning("Semantic compilation failed: %s", e)
        return None, CompileMiss("guardrail_rejected", str(e)[:200])


def make_query_sketch(
    llm: LLMGateway,
    config: AgentConfig,
    agentic: bool = True,
    connectors=None,
    semantic_layer=None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the query_sketch node bound to an LLM gateway."""

    async def query_sketch(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}

        # 方言从活跃数据源 adapter 解析(与 gen_sql 同款):state.dialect
        # 默认为 "sqlite",fast_match 未命中时不会回写,query_sketch 直接用默认
        # 方言会把 MySQL 等库编译成 sqlite 语法(strftime 等)导致执行失败。
        dialect = state.dialect
        if connectors:
            try:
                adapter = await connectors.get(state.datasource or None)
                dialect = adapter.dialect()
            except Exception:
                pass

        # 回退重跑：携带上一次失败与诊断，重定计划而不是重写原计划
        base_correction = " ".join(
            p for p in (state.error_feedback, state.error_analysis, state.reason) if p
        )
        schema_map = await _schema_map(connectors, state.datasource or None)
        # 计划起草走 fast 档(未配置 fast → 回退 target)
        model = (
            config.node_models.get("query_sketch")
            or config.model_fast
            or config.target
            or "openai/gpt-4o"
        )
        system_prompt = render(
            "query_sketch/system",
            lang=state.lang,
        )
        # 方法论 skill:按节点确定性匹配(manifest.yml),注入 system prompt
        skill_block = render_skills("query_sketch", lang=state.lang)
        if skill_block:
            system_prompt = f"{system_prompt}\n\n{skill_block}"
        llm_detail: dict[str, Any] | None = None

        async def call_query_sketch(correction: str) -> str:
            nonlocal llm_detail
            prompt = render(
                "query_sketch/user",
                lang=state.lang,
                question=state.question,
                schema_context=state.schema_context[:10000],
                evidence=state.evidence,
                time_context=state.time_context,
                history=state.history,
                correction=correction[:600] if correction else "",
                previous_plan=state.plan[:800] if state.plan else "",
            )
            # 语义优先(Phase B,决策 1):query_sketch 不再暴露任何 catalog 探测工具——
            # get_table_columns / get_column_stats 已从查询路径物理移除
            # (agent 运行时不可能触达物理元数据)。

            start = time.monotonic()
            try:
                response = await llm.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    metadata={
                        "node": "query_sketch",
                        "session_id": state.session_id,
                        "run_id": state.run_id,
                        "question": state.question[:80],
                    },
                    # 结构化输出:强约束"只输出 JSON"(替代正则剥围栏)。部分
                    # provider 不支持 response_format → 捕获后不带它重试一次。
                    response_format={"type": "json_object"},
                )
            except Exception:
                response = await llm.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    metadata={
                        "node": "query_sketch",
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
            raw = await call_query_sketch(base_correction)
            plan_json = _parse_plan(raw)
            plan = _plan_text(raw, state.lang)
            errors = validate_plan(plan_json, schema_map)
            if errors:
                fix_correction = (
                    base_correction
                    + f" Your previous plan was invalid: {'; '.join(errors)}. "
                    + "Fix the plan so every table and column reference exists in the schema."
                )
                raw = await call_query_sketch(fix_correction)
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
            # query_sketch 常把实体计数误译成 count(loan.loan_id) 的记录计数,这里
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
            # P1-4:解析出的时间范围确定性绑定唯一声明时间维度(注入 plan.conditions)。
            # 覆盖内问题 → 编译 SQL 必然带时间过滤;未覆盖 → gen_sql 的 plan
            # 文本带该条件。无法判定(多时间字段/无时间字段)不猜,time_context
            # 仍作为 prompt 文本传给 LLM 自行处理。
            if state.time_context and semantic_layer is not None:
                timed = _inject_time_condition(
                    plan_json, state.time_context,
                    semantic_layer.model(), state.matched_tables,
                )
                if timed is not None:
                    plan_json = timed
                    plan = _render_plan(plan_json, state.lang)
            update = {
                "plan": plan,
                "plan_json": plan_json,
                "plan_validation": {"status": "ok"},
                "dialect": dialect,
            }
            # 受限选择编译:覆盖内问题编译器拼出权威 SQL 并注入 plan,
            # gen_sql 遵从(确定性通道);MISS → 拒绝(语义优先唯一通道)。
            # 先在解析边界强类型化(形态错误 → None → 回退 raw-dict 流,
            # 与改造前字节级一致)。
            plan_query = parse_plan_query(plan_json)
            compiled, miss = _compile_semantic(
                plan_query if plan_query is not None else plan_json,
                state.matched_tables, semantic_layer, dialect,
            )
            # 编译决策观测:恒写(命中/MISS/短路),eval hit-rate 归因闭环。
            compile_meta = {
                "outcome": "compiled" if compiled is not None else "miss",
                "plan_typed": plan_query is not None,
                "semantic_layer": semantic_layer is not None,
            }
            if compiled is not None:
                compile_meta.update(miss_reason="", miss_component="")
            elif miss is not None:
                if semantic_layer is None:
                    # 短路真实分因(无语义层 ≠ no_plan):eval 接线诊断用
                    compile_meta.update(
                        miss_reason="no_semantic_layer", miss_component="")
                else:
                    compile_meta.update(
                        miss_reason=miss.reason, miss_component=miss.component)
            else:
                compile_meta.update(miss_reason="unknown", miss_component="")
            update["compile_meta"] = compile_meta
            if compiled is not None:
                compiled_sql, block = compiled
                update["plan"] = f"{plan}\n\n{block}" if plan else block
                update["compiled_sql"] = compiled_sql
                update["compiled"] = True
            else:
                # 语义优先(Phase B,决策 4):编译 MISS 不再静默降级裸表——
                # 计划有真实意图但组件未覆盖 → 拒绝信号,图路由到 refuse 节点
                # (LLM 草拟扩展 draft → 管理端确认 → 重答)。退化/空洞计划
                # 不拒绝,gen_sql 从 semantic_context 照常生成。
                if _plan_has_intent(plan_json):
                    refusal = {
                        "reason": "uncovered",
                        "question": state.question,
                        "plan": plan_json,
                        "plan_text": plan,
                    }
                    if miss is not None:
                        # 结构化分因透出:reason slug + 失败组件,refuse 展示 /
                        # eval 聚合「编译覆盖率真实缺口」的数据源。
                        refusal["compile_miss"] = {
                            "reason": miss.reason,
                            "component": miss.component,
                        }
                        logger.info(
                            "compile miss for %r: %s (%s)",
                            state.question[:80], miss.reason, miss.component,
                        )
                    update["refusal"] = refusal
            if llm_detail:
                update["llm"] = llm_detail
            return update
        except Exception as e:
            logger.warning("Query-sketch failed (proceeding without a plan): %s", e)
            return {}

    return query_sketch
