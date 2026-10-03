"""Query-sketch node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Query-sketch failures are silent (empty plan): the
pipeline never blocks on planning.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.prompts.skills import append_skill_block, render_skills
from trove.services.semantic_layer import rls
from trove.services.semantic_layer.compiler import (
    CompileMiss,
    CompileResult,
    PartialCompile,
    is_hard_miss,
)
from trove.services.semantic_layer.contract import (
    contract_to_wire,
    render_contract,
)
from trove.services.semantic_layer.plan import PlanQuery, parse_plan_query
from trove.workflow.state import WorkflowState, budget_exhausted

logger = get_logger(__name__)

# 时间粒度中文标签(渲染 zh plan 文本用;编译器消费原始 grain slug)
_GRAIN_ZH = {"year": "年", "quarter": "季度", "month": "月", "week": "周", "day": "日"}

# 「计划自相矛盾」的错误前缀:硬 MISS 里缺的组件**语义模型里其实有**,是
# 计划自己没带上/没写对 —— 修法是**重新规划**,不是拒绝(与「模型缺口」
# 分开,后者照旧拒绝 + 反问扩展模型)。
PLAN_CONTRADICTION_TAG = "[ERR:PLAN_CONTRADICTION]"

#: 单题重规划轮数上限(与共享修正预算叠加,两道闸都要过)。
MAX_PLAN_REPLANS = 2


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


def _ordering_text(ordering: Any) -> str:
    """排序渲染:强类型 dump(list[dict])与模型直出的字符串都能读。

    A1-10 之后 plan 文本由强类型对象渲染,ordering 已归一为
    ``[{"column","direction"}]``;``str(list)`` 会把它渲染成 Python repr
    (带引号的字典字面量),对 gen_sql 是噪音,这里显式拼成 ``col dir``。
    旧形态(字符串/list[str])仍照旧渲染——渲染器不设形状前提。
    """
    if isinstance(ordering, str):
        return ordering
    if isinstance(ordering, list):
        parts: list[str] = []
        for o in ordering:
            if not isinstance(o, dict):
                parts.append(str(o))
                continue
            col = str(o.get("column") or "").strip()
            if col:
                parts.append(f"{col} {str(o.get('direction') or 'asc').strip()}")
        return ", ".join(parts)
    return str(ordering)


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
        agg = data["aggregation"]
        # 多聚合列表形态(模型偶发)→ 按表达式逐项渲染,别把 Python repr
        # 塞进 gen_sql 的提示词(它照计划写 SQL,repr 是纯噪音)
        text = ", ".join(str(a) for a in agg) if isinstance(agg, list) else str(agg)
        lines.append(("聚合: " if zh else "Aggregation: ") + text)
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
        rank = extreme.get("rank")
        # rank 渲染给 gen_sql:它是"第 N 高/低"的结构信号(编译器据此产出
        # 选择谓词),gen 照计划文本构造 SQL 时需要看到它。
        rank_txt = f" · rank: {rank}" if rank else ""
        lines.append(
            f"{('极值: ' if zh else 'Extreme: ')}{extreme.get('func')}({extreme.get('column')})"
            f" · scope: {scope}{rank_txt}"
        )
    if data.get("ordering"):
        lines.append(("排序: " if zh else "Ordering: ") + _ordering_text(data["ordering"]))
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


def _prose(response: str) -> str:
    """LLM 回复的散文回退(剥掉可能的 markdown 围栏)。"""
    text = (response or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    return (m.group(1) if m else text).strip()


def _typed_plan(response: str) -> PlanQuery | None:
    """LLM 回复 → 强类型计划。**这里是计划形状的唯一判定点**。

    经 ``PlanQuery`` 校验后才算"计划":能进编译器(它只吃 PlanQuery)、能跑
    列检查(那些读 ``answer_columns``)、能确定性渲染。JSON 解析失败**或**
    形状不是计划(``parse_plan_query`` → None)→ 返回 None,调用方把原始文本
    当散文注入 gen_sql,并把这件降级记出来。

    改造前这里只做 JSON 解析(``_parse_plan``),形状从不校验 —— 于是"模型
    输出了一坨 JSON"和"模型给了一份计划"在下游完全同形。
    """
    data = _parse_plan(response)
    return parse_plan_query(data) if data is not None else None


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
    # 极值计划(extreme 带列)的指标列由编译器消费 extreme 生成(rank=1 的
    # 排序/rank≥2 的选择谓词),注入 FUNC(*) 只会多出一个通配占位列——0475
    # 实测把"单列实体"答案变成"实体列 + max(*)"两列。
    extreme = plan.get("extreme")
    if isinstance(extreme, dict) and str(extreme.get("column") or "").strip():
        return None
    agg = str(plan.get("aggregation") or "").strip().lower()
    if not agg or agg in ("none", "无"):
        return None
    cols = [str(a).strip() for a in (plan.get("answer_columns") or [])]
    if cols and any("(" in a for a in cols):
        return None  # 已有聚合表达式列 → 不重复补
    # aggregation 可能带修饰(如 "count(distinct x)")——取其函数名作占位前列
    func = re.split(r"[(\s]", agg, 1)[0] or "count"
    if not func.lower().startswith("count"):
        # 非 count 族的聚合(avg/sum/min/max)注入 FUNC(*) 是非法或语义错的
        # 占位:AVG(*)/SUM(*) 在 MySQL 语法非法,MIN(*)/MAX(*) 语义是"任意行
        # 极值"而非计划声明的度量(如 avg(order.amount))。这类计划的指标列
        # 由 aggregation 表达式本身承载,gen_sql 照计划文本输出即可。
        return None
    if plan.get("having"):
        # 已有聚合后过滤(having)→ 计划已把量级约束表达完整(0494 型:"in
        # total 3539" 的 HAVING),通配占位列只会改变结果宽度。
        return None
    metric = f"{func}(*)"
    fixed = dict(plan)
    fixed["answer_columns"] = list(cols) + [metric]
    fixed["plan_field"] = "ensure_aggregate_answer_column"
    return fixed


#: 序数词 → extreme rank(「第 N 高/低」)。中英并收,词边界匹配;
#: 只认**恰一个**命中——多命中=歧义,不猜。
_ORDINAL_RANKS = {
    "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
}
_ORDINAL_RANKS_ZH = {
    "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def extreme_rank_from_question(
    plan: dict[str, Any] | None, question: str,
) -> dict[str, Any] | None:
    """「第二高/third-highest」型问句 → 给 plan.extreme 补 rank(零 LLM)。

    编译器消费 extreme.rank:rank≥2 产出「第 N 高/低」的选择谓词,缺省 1。
    planner 常把 "second-highest" 写进 scope 文本却不落结构字段——这里按
    问句词形确定性补齐,让编译器不必解析自由文本。恰一个序数词命中才认。
    """
    if not isinstance(plan, dict):
        return None
    extreme = plan.get("extreme")
    if not isinstance(extreme, dict) or not str(extreme.get("column") or "").strip():
        return None
    if extreme.get("rank") is not None:
        return None  # planner 已给 → 不动(显式优先)
    ql = (question or "").lower()
    hits: set[int] = set()
    for word, rank in _ORDINAL_RANKS.items():
        if re.search(rf"\b{re.escape(word)}\b", ql):
            hits.add(rank)
    for word, rank in _ORDINAL_RANKS_ZH.items():
        if f"第{word}" in (question or ""):
            hits.add(rank)
    if len(hits) != 1:
        return None
    fixed = dict(plan)
    fixed_extreme = dict(extreme)
    fixed_extreme["rank"] = hits.pop()
    fixed["extreme"] = fixed_extreme
    fixed["plan_field"] = "extreme_rank_from_question"
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

    # 实体候选表(问题名词 → plan 表),同时作精确层的外键护栏:改写只认
    # "记录表 → 问题点名的实体表"的所有权边。外键那端是维度表
    # (account.district_id = district.district_id)时,去重计数会把每个
    # 分组塌成 1(0483 实测误改 → count(distinct account.district_id))。
    entity_hint = set(_entity_tables(question, plan, lang))
    expr = _distinct_expr_from_plan(colses, joins, entity_hint)
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


def _distinct_expr_from_plan(
    cols: list[str], joins: str, entity_tables: set[str],
) -> str | None:
    """精确层:从 answer_columns 的 count(记录表.列) + joins 外键推去重实体列。

    只认 ``count(<t>.<anything>)`` 且 joins 里有 ``<t>.<fk> = <other>.<id>``
    **且 <other> 属于问题实体候选表**:把记录计数(count 行)改成
    count(distinct 记录表.外键列)。外键列名通常即"实体归属",如
    count(loan.loan_id) → count(distinct loan.account_id)。

    护栏(0483 实测):``<other>`` 是维度表(district)时该边不是所有权边,
    去重计数会把每个分组塌成 1——不认,继续找该表的其它边;都没有则
    返回 None(交兜底层/planner 纪律,不瞎改)。
    """
    def _fk_edge(tbl: str) -> str | None:
        for fk in re.finditer(
            rf"\b{re.escape(tbl)}\s*\.\s*(\w+)\s*=\s*([A-Za-z_][\w]*)\s*\.\s*(\w+)",
            joins, re.I,
        ):
            if fk.group(2).lower() in entity_tables:
                return fk.group(1)
        return None

    for a in cols:
        m = re.match(r"^count\s*\(\s*([A-Za-z_][\w]*)\.(\w+)\s*\)", a, re.I)
        if not m:
            continue
        tbl, col = m.group(1), m.group(2)
        if col.lower() == f"{tbl}_id".lower():
            continue  # count(loan.loan_id) 是记录主键,不是外键;继续找外键
        # joins 里该表的其它列作为 <=> 键(通常是外键,如 account_id)
        fk_col = _fk_edge(tbl)
        if fk_col:
            return f"count(distinct {tbl}.{fk_col})"
    # 记录主键在 count 里,退一层:从 joins 找记录表级联的外键
    for a in cols:
        m = re.match(r"^count\s*\(\s*([A-Za-z_][\w]*)\.\w+\s*\)", a, re.I)
        if not m:
            continue
        fk_col = _fk_edge(m.group(1))
        if fk_col:
            return f"count(distinct {m.group(1)}.{fk_col})"
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
    # 计划自己声明了 time_grain 的字段必然渲染成分桶列,而且常带别名
    # (``DATE_FORMAT(loan.date,'%Y') AS year``)——结果列名里再也找不到该
    # 字段的字面引用。它与含 `(` 的表达式同属"按名字对不上"的一类,从 refs
    # 摘掉:留着它会把一条**忠实实现了计划**的 SQL 判成背离计划,而代价不是
    # 一次重试——回退重跑只会重生同一个计划,爬完梯子就优雅降级,把一个已经
    # 算对的结果丢掉。只有 field 没有 grain 是半截声明,不算分桶,照旧对账。
    tg = plan_json.get("time_grain")
    if isinstance(tg, dict) and str(tg.get("grain") or "").strip():
        field = str(tg.get("field") or "").strip().lower()
        if field:
            refs = [r for r in refs if r.lower() != field]
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


def _datasets_by_alias(model) -> dict[str, Any]:
    """数据集查找表:数据集名与物理表名两个方向都指向同一个 dataset。"""
    by_table: dict[str, Any] = {}
    for ds in model.datasets:
        for alias in (ds.name, rls.physical_table(ds)):
            if alias and str(alias).strip():
                by_table[str(alias).strip().lower()] = ds
    return by_table


def _table_anchors(name: str, by_table: dict[str, Any]) -> set[str]:
    """一个表名的全部别名(小写):原名 + 该数据集的声明名 + 物理表名。"""
    out = {str(name).strip().lower()}
    ds = by_table.get(str(name).strip().lower())
    if ds is not None:
        out.add(ds.name.strip().lower())
        out.add(rls.physical_table(ds))
    return out


def _declared_join_clauses(model, tables: list[str]) -> list[str]:
    """``tables`` 之间已声明的关系子句(``a.col = b.col``,官方路径写法)。

    plan.joins 的合法写法就是这串子句(编译器按声明列对校验,见
    ``_explicit_join_edges``);反馈给 planner 的必须是**声明里已有的**那条,
    不是它自己编的等值边。
    """
    by_table = _datasets_by_alias(model)
    anchors: set[str] = set()
    for t in tables:
        anchors |= _table_anchors(t, by_table)
    clauses: list[str] = []
    for r in model.relationships or []:
        if not (
            (_table_anchors(r.from_, by_table) & anchors)
            and (_table_anchors(r.to, by_table) & anchors)
        ):
            continue
        for fc, tc in zip(r.from_columns or [], r.to_columns or []):
            clauses.append(f"{r.from_}.{fc} = {r.to}.{tc}")
    return clauses


def _declared_metric_names(model, tables: list[str], limit: int = 8) -> list[str]:
    """与计划相关的已声明度量名(≤limit):排序候选的可执行素材。

    相关 = 度量锚定数据集命中 plan.tables(别名口径同 ``_datasets_by_alias``)。
    一个都不相关时按声明序兜底 —— 退化方向选「给得出名字」:反馈文本是
    planner 的唯一额外输入,给空的候选列表等于没给。
    """
    by_table = _datasets_by_alias(model)
    anchors: set[str] = set()
    for t in tables:
        anchors |= _table_anchors(t, by_table)
    relevant = [
        m.name
        for m in model.metrics
        if m.name and any(
            str(d).strip().lower() in anchors for d in (m.datasets or [])
        )
    ]
    if not relevant:
        relevant = [m.name for m in model.metrics if m.name]
    return relevant[:limit]


def _ordering_candidates(plan_json, semantic_layer) -> tuple[list[str], list[str]]:
    """(声明度量名, 计划的聚合列表达式)—— limit_without_order 的候选形态。"""
    exprs = [
        str(ac).strip()
        for ac in (plan_json or {}).get("answer_columns") or []
        if "(" in str(ac) and str(ac).strip()
    ]
    metrics: list[str] = []
    if semantic_layer is not None:
        try:
            model = semantic_layer.model()
        except Exception:
            model = None
        if model is not None:
            tables = [
                str(t).strip() for t in (plan_json or {}).get("tables") or []
            ]
            metrics = _declared_metric_names(model, tables)
    return metrics, exprs


def _replan_feedback(
    plan_json: dict[str, Any] | None,
    miss: CompileMiss | None,
    semantic_layer,
) -> str | None:
    """硬 MISS 二分:「计划自相矛盾」的确定性判定 + 重规划反馈(零 LLM)。

    返回 None = 真模型缺口(照旧拒绝 + 反问扩展模型)。白名单判定,新分因
    默认 None = 保持旧行为:
      - unreachable_table:缺失表**全部**在语义模型里声明过 —— 模型有、
        计划没带。反馈必须带上该数据集的**声明关系与字段清单**:那轮
        planner 的 schema_context 里根本没有匹配到它(linker 没给),只喊
        「加进 plan.tables」它无从下手(0483 实测)。
      - limit_without_order:计划形状缺陷(limit 在、ordering 不可解析)
        —— 重规划可修。光说「补 ordering」不够(0487 实测重规划空转):
        附上**可写进 ordering 的具体形态**(相关声明度量名 / 计划的聚合列
        表达式 / dataset.field asc|desc),planner 才有可执行素材。
      - ambiguous_join_path:组件引用的列都在已声明关系里,但 root→表的
        声明路径不唯一(菱形共享维度)。修法是**整份重计划 + 显式
        plan.joins 写官方路径**——只让 LLM 重写被质疑的那一段,它会改出
        第三条同样二义的路径。
    反馈文本英文、≤600 字符、指令在前(correction 有 ``[:600]`` 截断)。
    """
    if miss is None:
        return None
    reason = getattr(miss, "reason", "") or ""
    component = getattr(miss, "component", "") or ""
    if reason == "limit_without_order":
        metrics, exprs = _ordering_candidates(plan_json, semantic_layer)
        text = (
            f"{PLAN_CONTRADICTION_TAG} The plan sets a row limit without a "
            f"resolvable ordering ({component or 'missing'}). Re-emit the whole "
            "plan JSON with ordering written as one of: a declared metric name; "
            'an explicit "<dataset>.<field> asc|desc"; or an aggregate '
            "expression already present in answer_columns. Otherwise drop the "
            "limit."
        )
        if metrics:
            text += " Declared metrics: " + ", ".join(metrics) + "."
        if exprs:
            text += " Plan aggregate expressions: " + ", ".join(exprs) + "."
        return text[:600]
    if reason == "ambiguous_join_path":
        if semantic_layer is None:
            return None
        try:
            model = semantic_layer.model()
        except Exception:
            return None
        if model is None:
            return None
        tables = [str(t).strip() for t in (plan_json or {}).get("tables") or []]
        clauses = _declared_join_clauses(model, tables)
        text = (
            f"{PLAN_CONTRADICTION_TAG} The plan's columns reference declared "
            "relations, but the declared join path between them is ambiguous "
            "(more than one route). Re-emit the **whole** plan JSON with "
            "plan.joins written out as the official declared path(s) — one "
            "'table.column = table.column' clause per hop, separated by ';'."
        )
        if clauses:
            text += " Declared clauses: " + "; ".join(clauses) + "."
        return text[:600]
    if reason != "unreachable_table" or semantic_layer is None:
        return None
    missing = [
        t.strip() for t in component.split(":", 1)[-1].split(",") if t.strip()
    ]
    if not missing:
        return None
    model = None
    try:
        model = semantic_layer.model()
    except Exception:
        return None
    if model is None:
        return None
    declared = {d.lower() for d in rls.declared_tables(model)}
    if any(t.lower() not in declared for t in missing):
        return None  # 未声明表 → 真模型缺口,照旧拒绝
    # 数据集映射(数据集名/物理表名两个方向):关系与字段清单从这里取。
    by_table = _datasets_by_alias(model)

    def _anchors(name: str) -> set[str]:
        return _table_anchors(name, by_table)

    plan_tables = [str(t).strip() for t in (plan_json or {}).get("tables") or []]
    plan_anchors: set[str] = set()
    for t in plan_tables:
        plan_anchors |= _anchors(t)
    missing_lower = {t.lower() for t in missing}
    parts: list[str] = []
    for t in missing:
        ds = by_table.get(t.lower())
        if ds is None:
            continue
        rels = [
            r
            for r in (model.relationships or [])
            if ((missing_lower & _anchors(r.from_)) and (plan_anchors & _anchors(r.to)))
            or ((missing_lower & _anchors(r.to)) and (plan_anchors & _anchors(r.from_)))
        ]
        joins = "; ".join(
            f"{r.from_}.{fc} = {r.to}.{tc}"
            for r in rels
            for fc, tc in zip(r.from_columns, r.to_columns)
        )
        fields = ", ".join(f.name for f in ds.fields if f.name)
        detail = f"declared joins: {joins}" if joins else "no declared relationship"
        if fields:
            detail += f"; declared fields: {fields}"
        parts.append(f"{ds.name} ({detail})")
    text = (
        f"{PLAN_CONTRADICTION_TAG} Plan is self-contradictory: it references "
        f"table(s) [{', '.join(missing)}] that ARE declared in the semantic "
        "model but missing from plan.tables. Re-emit the whole plan JSON: "
        "either add them to plan.tables (using the declared joins) or drop "
        "the conditions referencing them."
    )
    if parts:
        text += " Declared: " + " | ".join(parts)
    return text[:600]


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
    allowed_tables: set[str] | None = None,
) -> tuple[CompileResult | PartialCompile | None, CompileMiss | None]:
    """语义层覆盖内 → ((权威 SQL, 提示块), None);MISS → (None, CompileMiss)。

    受限选择编译:plan 的 metric/group_by/filters 必须全部解析到已声明
    metric/field/relationship,AND guardrail 放行才注入 gen_sql;否则原样
    走现有通道。全程确定性,零额外 LLM 调用。miss reason(结构化分因)
    带出供 query_sketch/refuse/eval 归因——不再被丢弃成笼统「uncovered」。

    分级逃生梯(soften over-rejection):软 MISS(词表/值/口径未声明)不整体
    拒绝——编译器产出 ``PartialCompile`` 骨架(可解析部分权威化),消费方
    注入 gen_sql 让 LLM 补缺并照常回答;只有硬 MISS(结构性:fan-out/二义/
    未覆盖表/坏定义)才返回 CompileMiss 触发 refuse。

    入参可为强类型 PlanQuery(query_sketch 解析后的 AST)或 raw dict——
    编译器内部统一按 dict 流处理。强类型入参另做一件事:产物带回
    ``source_plan``(A1-9 引用同一性),调用方据此知道这份 SQL 编译自
    哪份计划;raw dict 入参(旧调用方/兜底)带回 ``None``。dialect 来自
    state(适配器),驱动时间分桶等方言感知渲染。
    """
    from trove.services.semantic_layer.compiler import (
        SemanticCompiler,
        validate_compiled_sql,
    )

    typed = plan if isinstance(plan, PlanQuery) else None
    if typed is not None:
        plan = typed.to_dict()
    if plan is None or not matched or semantic_layer is None:
        return None, CompileMiss("no_plan_or_matched", "")
    try:
        model = semantic_layer.model()
        if model is None:
            return None, CompileMiss("no_plan_or_matched", "no semantic model")

        result = SemanticCompiler(
            model, allowed_tables=allowed_tables,
        ).compile_detailed(
            plan, list(matched), force_dialect=dialect)
        if isinstance(result, CompileMiss):
            return None, result
        violations = validate_compiled_sql(result.sql, model, list(matched))
        if violations:
            logger.info(
                "Compiled SQL rejected by guardrail: %s", "; ".join(violations))
            return None, CompileMiss(
                "guardrail_rejected", "; ".join(violations))
        return replace(result, source_plan=typed), None
    except Exception as e:
        logger.warning("Semantic compilation failed: %s", e)
        return None, CompileMiss("guardrail_rejected", str(e)[:200])


def make_query_sketch(
    llm: LLMGateway,
    config: AgentConfig,
    agentic: bool = True,
    connectors=None,
    semantic_layer=None,
    skills=None,
    max_retries: int = 10,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the query_sketch node bound to an LLM gateway.

    Args:
        skills: Optional SkillService — when present, merges admin-managed
            org methodology skills (required tier) into the system prompt;
            absent → only the built-in code skills (backward compatible).
        max_retries: Shared correction budget (与 execute_sql 同一份)——
            计划自相矛盾的有界重规划要过预算闸(另一道闸是 MAX_PLAN_REPLANS)。
    """

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
        # 归因意图:追加归因计划指导(为什么/贡献/根因问题)——主线 SQL 仍
        # 正常规划,plan_json 里额外带 "attribution" 块供归因节点多跳下钻。
        if state.intent == "attribution":
            system_prompt = (
                f"{system_prompt}\n\n{render('query_sketch/attribution', lang=state.lang)}"
            )
        # 方法论 skill:按节点确定性匹配(manifest.yml),注入 system prompt;
        # 有 SkillService 时合并 org 技能(required 档全量注入)。
        skill_block = (
            skills.render_skills("query_sketch", **state.skill_ctx())
            if skills is not None
            else render_skills("query_sketch", **state.skill_ctx())
        )
        system_prompt = append_skill_block(system_prompt, skill_block)
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
            plan_query = _typed_plan(raw)
            plan_json = plan_query.to_dict() if plan_query is not None else None
            plan = _render_plan(plan_json, state.lang) if plan_query is not None else _prose(raw)
            errors = validate_plan(plan_json, schema_map)
            if errors:
                fix_correction = (
                    base_correction
                    + f" Your previous plan was invalid: {'; '.join(errors)}. "
                    + "Fix the plan so every table and column reference exists in the schema."
                )
                raw = await call_query_sketch(fix_correction)
                plan_query = _typed_plan(raw)
                plan_json = plan_query.to_dict() if plan_query is not None else None
                plan = (
                    _render_plan(plan_json, state.lang)
                    if plan_query is not None else _prose(raw)
                )
                errors = validate_plan(plan_json, schema_map)
            if errors:
                logger.info("Plan dropped after validation: %s", "; ".join(errors))
                update: dict[str, Any] = {
                    "plan": "",
                    "plan_json": None,
                    "plan_validation": {"status": "dropped", "errors": errors},
                    "plan_replan_pending": False,
                }
                if llm_detail:
                    update["llm"] = llm_detail
                return update
            if not plan:
                return {"plan_replan_pending": False}
            # 形态不合 typed IR 的计划(模型输出 JSON 但结构不是计划)降级为
            # 散文:它进不了编译(编译器只吃 PlanQuery),也跑不了列检查(那些
            # 读 answer_columns)。降级可以,但不能无声——下面按 plan_typed
            # 记进 compile_meta,validate 节点再按 plan_json is None 记一笔。
            if plan_query is None:
                logger.info(
                    "plan is not a typed plan (prose fallback) for %r",
                    state.question[:80],
                )
            # 语义级计数纠正(优先):「X 的用户数量/人数」→ count(distinct 实体)。
            # query_sketch 常把实体计数误译成 count(loan.loan_id) 的记录计数,这里
            # 在 plan→gen 之间确定性纠偏——gen 遵守规则 19 也不会做错。
            # 先于 ensure_aggregate_answer_column:后者只补 count(*) 占位列,
            # 若先跑会把已纠正的去重语义覆盖成 count(*) 通配。
            corrected = correct_entity_count_plan(plan_json, state.question, state.lang)
            if corrected is not None:
                plan_json = corrected
                plan = _render_plan(plan_json, state.lang)
            # 极值序数词补齐:「第二高/third-highest」→ plan.extreme.rank
            # (编译器据此产出第 N 高/低的选择谓词;缺省 rank=1 走排序形态)。
            # 确定性、恰一命中才认;和上面一样先纠正再渲染、再重解析。
            ranked = extreme_rank_from_question(plan_json, state.question)
            if ranked is not None:
                plan_json = ranked
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
            # 纠正是确定性变换(dict → dict),shape 不变——所以重解析对
            # canonical dict 是幂等的(三个纠正函数只写聚合表达式/answer_columns/
            # conditions/plan_field,全是 IR 认的形状)。上面若发生过降级,
            # plan_json 为 None,这里也仍为 None。
            if plan_json is not None:
                plan_query = parse_plan_query(plan_json)
            update = {
                "plan": plan,
                "plan_json": plan_json,
                "plan_validation": {"status": "ok"},
                "dialect": dialect,
                # 每次运行先复位重规划信号;下面的发射分支会再置位。
                "plan_replan_pending": False,
            }
            # 归因意图:plan_json 里的 "attribution" 块(目标指标/维度/基准/
            # 深度)带出到状态,供 reflect OK 后 attribution 节点多跳下钻。
            # 编译器按 extra=ignore 忽略该未知键,主线 SQL 规划不受影响。
            attr_block = plan_json.get("attribution") if isinstance(plan_json, dict) else None
            if state.intent == "attribution" and isinstance(attr_block, dict) and attr_block:
                update["attribution_plan"] = attr_block
            # 受限选择编译:覆盖内问题编译器拼出权威 SQL 并注入 plan,
            # gen_sql 遵从(确定性通道);MISS → 拒绝(语义优先唯一通道)。
            # 只递强类型计划(A1-10 双路合一):plan_query 为 None ⟺ plan_json
            # 为 None(散文档),此时没什么可编译的,没必要再拿松 dict 试一次。
            # 执行期表授权前移:取该数据源 allowlist(与 registry 执行守卫同一
            # 份)交给编译器,越界数据集编译期即 MISS。
            allowed_tables = None
            if connectors is not None:
                try:
                    _ds = state.datasource or getattr(connectors, "default_name", None)
                    if _ds:
                        allowed_tables = connectors.allowed_tables(_ds)
                except Exception:
                    allowed_tables = None
            compiled, miss = _compile_semantic(
                plan_query, state.matched_tables, semantic_layer, dialect,
                allowed_tables=allowed_tables,
            )
            # 引用同一性(A1-9):产物须带回它编译自的那份计划。None 而
            # plan_json 非 None = 某条路径把 IR 不认的形状喂进了编译器——
            # 那时列检查/复杂度读的 dict 与编译用的形状不是一份东西,回答
            # 照常交付但这件事必须说出来(降级可以说,但不能不说)。
            if compiled is not None and compiled.source_plan is None and plan_json is not None:
                logger.warning(
                    "compiled SQL without a source plan identity for %r "
                    "(compiled from an unvalidated plan shape)",
                    state.question[:80],
                )
            # 编译决策观测:恒写(compiled/partial/miss),eval hit-rate 归因闭环。
            # 软 MISS 不再整体拒绝——编译器产出 PartialCompile 骨架,回答照常
            # 交付,未覆盖组件清单(partial_reasons)供学习与归因。
            is_partial = isinstance(compiled, PartialCompile)
            compile_meta = {
                "outcome": (
                    "partial"
                    if is_partial
                    else ("compiled" if compiled is not None else "miss")
                ),
                "plan_typed": plan_query is not None,
                "semantic_layer": semantic_layer is not None,
            }
            if is_partial:
                compile_meta["partial_reasons"] = [
                    m.get("reason") for m in compiled.miss_parts
                ]
            if compiled is not None:
                compile_meta.update(miss_reason="", miss_component="")
            elif miss is not None:
                # 硬度分级(A2):只有硬 MISS 触发拒绝;软 MISS 直通 gen_sql。
                # 与 outcome 正交,additive —— 值域冻结的是 outcome 本身。
                compile_meta["miss_class"] = (
                    "hard" if is_hard_miss(miss.reason) else "soft")
                if semantic_layer is None:
                    # 短路真实分因(无语义层 ≠ no_plan):eval 接线诊断用
                    compile_meta.update(
                        miss_reason="no_semantic_layer", miss_component="")
                else:
                    compile_meta.update(
                        miss_reason=miss.reason, miss_component=miss.component)
            else:
                compile_meta.update(miss_reason="unknown", miss_component="")
                compile_meta["miss_class"] = "hard"
            update["compile_meta"] = compile_meta
            # 硬 MISS 二分(分级逃生梯的上沿):「计划自相矛盾」——缺的组件
            # 语义模型里其实有,是计划自己没带上/没写对——先给一次**有界
            # 重规划**(反馈进 correction,携带声明关系与字段清单),耗尽
            # 才落回拒绝。真模型缺口(未声明表等)不进这条路,照旧直接拒绝。
            if (
                compiled is None
                and miss is not None
                and _plan_has_intent(plan_json)
                and not budget_exhausted(state.retry_count, max_retries)
                and state.plan_replan_rounds < MAX_PLAN_REPLANS
            ):
                replan = _replan_feedback(plan_json, miss, semantic_layer)
                if replan is not None:
                    logger.info(
                        "plan contradiction for %r: %s — replanning (%d/%d)",
                        state.question[:80], miss.component,
                        state.plan_replan_rounds + 1, MAX_PLAN_REPLANS,
                    )
                    update.update({
                        "error_feedback": replan,
                        "retry_count": state.retry_count + 1,
                        "plan_replan_rounds": state.plan_replan_rounds + 1,
                        "plan_replan_pending": True,
                        # 上一轮编译产物**必须清**:execute_sql 的保真校验读
                        # compiled/compiled_sql,残留旧权威 SQL 会让下一条
                        # SQL 被拿去和旧契约比对 → 假 COMPILE_DRIFT。
                        "compiled": False,
                        "compiled_sql": "",
                        "compile_partial": False,
                        "compile_misses": [],
                        "contract": None,
                    })
                    if llm_detail:
                        update["llm"] = llm_detail
                    return update
            if compiled is not None:
                # 注入文本是契约的纯渲染;对象本身随 wire 形状落到 state,
                # 供 execute_sql 的校验读取(不再从 SQL 字符串反推结构)。
                update["plan"] = (
                    f"{plan}\n\n{render_contract(compiled.contract)}"
                    if plan else render_contract(compiled.contract)
                )
                update["contract"] = contract_to_wire(compiled.contract)
                update["compiled_sql"] = compiled.sql
                update["compiled"] = True
                if is_partial:
                    # 分级逃生梯:软 MISS 组件注入状态(execute_sql 走骨架保真
                    # 校验;不置 refusal——本回答照常生成,不是硬停)。
                    update["compile_partial"] = True
                    update["compile_misses"] = list(compiled.miss_parts)
            else:
                # 语义优先(Phase B,决策 4)+ 硬度分流(A2,分级逃生梯的上沿):
                # 只有**硬 MISS**(结构性:fan-out/二义/未覆盖表/坏定义/
                # limit 无序)不静默降级裸表 —— 拒绝信号,图路由到 refuse
                # 节点(LLM 草拟扩展 draft → 管理端确认 → 重答)。退化/空洞
                # 计划不拒绝,gen_sql 从 semantic_context 照常生成。
                #
                # 软 MISS(词表/值/口径未声明)不拒绝:**缺陷在模型不在计划**
                # ——加一个 CASE WHEN / 多一句口径说明就能答,而拒绝会把「本可
                # 回答」的问题换成「先扩模型」。直通 gen_sql:plan 文本照常注入,
                # 不置 compiled/compile_partial/contract(没有骨架可保真)。
                # miss 缺席(编译器没给出分因)按硬处理——与 is_hard_miss 的
                # 「未知分因默认硬」同一条保守方向。
                if _plan_has_intent(plan_json) and (
                    miss is None or is_hard_miss(miss.reason)
                ):
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
                elif miss is not None:
                    logger.info(
                        "soft compile miss for %r: %s (%s) — passing through "
                        "to gen_sql (plan text injected as usual)",
                        state.question[:80], miss.reason, miss.component,
                    )
            if llm_detail:
                update["llm"] = llm_detail
            return update
        except Exception as e:
            logger.warning("Query-sketch failed (proceeding without a plan): %s", e)
            return {"plan_replan_pending": False}

    return query_sketch
