"""确定性简单快径——零 LLM、零网络:planner 之前用 kb init 模板分流。

模板命中 → 直接产出模板 SQL,跳过 planner + gen_sql(agent loop + KB
检索)+ multi-candidate + reflect 裁决;仍流经 execute_sql → select
(空候选透传)→ validate(确定性规则安全网)→ reflect(快径跳过,
见 reflect.py)。KB 防作弊约束:只用 kind='template'(kb init 确定性
产物),compose 组合候选在检索层已排除。

防错配是四重硬约束(全部满足才命中):
  1. SQL 形状(sqlglot):单表 FROM、无 JOIN/子查询/CTE/GROUP/ORDER/LIMIT、
     单个聚合、排除 `WHERE col > 0` 占位比较模板;
  2. 聚合意图词:问题含模板聚合函数对应的意图词(how many→COUNT 等);
  3. 表锚定:模板表 ∈ schema_linking 的 matched_tables,或表名(复数归一)
     出现在问题里;
  4. 族证据:模板措辞的 content token/年份字面量必须出现在问题里
     (防 "maximum amount" 模板命中 "maximum duration" 问题)。

miss 静默降级到正常链路——miss 成本(多一轮 LLM)远低于误命中成本(错 SQL)。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import sqlglot
from sqlglot import exp

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.observability import record_span
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import ExampleHit, KbService
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

FAST_PATH_MAX_QUESTION_LEN = 120  # 超过一律 miss(长问句交给正常链路)

_CJK_RE = re.compile(r"[一-鿿]")

# 聚合意图词:en 按词边界正则匹配短语,zh 按子串。短语越长优先(排序在编译时处理)。
_AGG_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "COUNT": (("how many", "total number", "number of", "count"),
              ("多少", "数量", "总数", "几条", "多少个", "记录数")),
    "AVG": (("average", "avg", "mean"), ("平均",)),
    "SUM": (("sum of", "sum", "total"), ("总和", "一共", "总计", "合计")),
    "MAX": (("most recent", "maximum", "highest", "largest", "biggest",
             "latest", "newest", "max"),
            ("最大", "最高", "最新", "最晚")),
    "MIN": (("minimum", "lowest", "smallest", "earliest", "oldest", "min"),
            ("最小", "最低", "最早")),
}
_EN_AGG_RE = {
    fn: re.compile(r"\b(" + "|".join(sorted(words, key=len, reverse=True)) + r")\b", re.I)
    for fn, (words, _) in _AGG_WORDS.items()
}
# 模板侧 token 剔除用的聚合词全集(跨族:模板措辞里的 how/many/maximum 等一律不要求)
_ALL_EN_AGG = frozenset(w for words, _ in _AGG_WORDS.values() for w in words)

# 结构词(问题/模板两侧剔除):how/many 等计数词在意图检查后不再要求
_STRUCT_EN = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "in", "with", "at", "to", "on",
    "by", "is", "are", "were", "was", "be", "been", "it", "its", "this", "that",
    "these", "those", "there", "here", "what", "which", "who", "whom", "whose",
    "how", "many", "do", "does", "did", "have", "has", "had", "can", "could", "would",
    "should", "may", "might", "per", "each", "not", "no", "yes",
    "records", "record", "row", "rows", "table", "tables", "count", "number",
    "me", "my", "i", "you", "we", "our", "their", "his", "her",
})
# date_range 模板的日期结构词(模板侧 token 剔除;方向/年份另有专项检查)
_DATE_STRUCT_EN = {"in", "on", "between", "before", "after", "and"}

# 年份/日期字面量
_YEAR_RE = re.compile(r"\b(\d{4})\b")
_DATE_LITERAL_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# zh 模板的 desc 提取(模板措辞固定,正则抠出中文片段)
_ZH_BARE_LABEL_RE = re.compile(r"^(.+?)表中")
_ZH_AGG_DESC_RE = re.compile(r"(?:最早|最晚)的(.+?)是什么时候|(.+?)的(?:最大|最小|平均|总和)值")
_ZH_DATE_DESC_RE = re.compile(r"中(.+?)(?:在|早于|晚于|为)\d")


def _norm_token(token: str) -> str:
    """复数归一(仅末尾 s;client/clients 视为同 token)。"""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _tokens(text: str) -> set[str]:
    # 下划线也分词("birth_date" → {birth, date}),列名与自然措辞才能对齐
    return {_norm_token(t) for t in re.findall(r"[a-z0-9]+", (text or "").lower())}


def _strip_agg_phrases(text: str) -> str:
    """把 en 聚合短语从文本中整体抹除(短语含空格,逐 token 剔除会漏)。"""
    pat = r"\b(" + "|".join(sorted(_ALL_EN_AGG, key=len, reverse=True)) + r")\b"
    return re.sub(pat, " ", text, flags=re.I)


def _has_agg_word(question: str, fn: str, is_zh: bool) -> bool:
    if is_zh:
        return any(w in question for w in _AGG_WORDS[fn][1])
    return bool(_EN_AGG_RE[fn].search(question))


def _desc_overlap(required: set[str], question_tokens: set[str]) -> bool:
    """desc 词锚定:共享 token,或共享 ≥4 字符前缀(词形变化:
    "approval date" 与 "approved" 不同 token,但语义同源)。"""
    if required & question_tokens:
        return True
    q4 = {t[:4] for t in question_tokens if len(t) >= 4}
    return any(t[:4] in q4 for t in required if len(t) >= 4)


def template_sql_shape_ok(sql: str) -> tuple[bool, str, str, bool]:
    """SQL 形状检查 → (ok, table_name, agg_func, has_where)。

    合格:单表 FROM、无 JOIN/子查询/CTE/GROUP/ORDER/LIMIT、单个聚合函数、
    无 `WHERE col > 0` 占位比较(阈值 0 是结构占位,不是数据锚定)。
    """
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:
        return False, "", "", False
    if not isinstance(tree, exp.Select):
        return False, "", "", False
    if any(tree.args.get(k) for k in ("with", "joins", "group", "order", "limit")):
        return False, "", "", False
    if tree.find(exp.Subquery):
        return False, "", "", False
    from_exprs = tree.args.get("from_") or []  # sqlglot: "from" 是关键字 → from_
    if not isinstance(from_exprs, list):
        from_exprs = [from_exprs]
    from_tables: list[exp.Table] = []
    for fe in from_exprs:
        from_tables.extend(fe.find_all(exp.Table))
    if len(from_tables) != 1:
        return False, "", "", False
    for cmp_ in (*tree.find_all(exp.GT), *tree.find_all(exp.GTE),
                 *tree.find_all(exp.LT), *tree.find_all(exp.LTE)):
        right = cmp_.expression
        if isinstance(right, exp.Literal) and right.this in ("0", "0.0"):
            return False, "", "", False
    aggs = list(tree.find_all(exp.AggFunc))
    if len(aggs) > 1:
        return False, "", "", False
    # sqlglot 的 Max/Avg 节点 .name 为空,函数名取类名("Max"→"MAX")
    agg = type(aggs[0]).__name__.upper() if aggs else ""
    return True, from_tables[0].name, agg, tree.args.get("where") is not None


def _table_anchored(table: str, question: str, matched_lower: set[str]) -> bool:
    """模板表锚定:表名(复数归一)∈ matched_tables。

    问题提及通道仅在 matched 多表(FK 邻居扩展)时启用——matched 单表
    且不含模板表说明 linker 认为该表无关,不得因问题提了词而放行。
    """
    t = _norm_token(table.lower())
    if t in {_norm_token(m) for m in matched_lower}:
        return True
    return len(matched_lower) > 1 and t in _tokens(question)


# ── 族特化匹配 ──────────────────────────────────────────


def _match_bare_count(hit: ExampleHit, question: str, table: str, is_zh: bool) -> bool:
    """裸 COUNT(无 WHERE):问题去掉结构词/计数词/表名后必须无剩余 token。

    "how many clients are there?" → 命中;"how many clients are male?"
    → 剩 male 拒绝(交给枚举模板);"how many clients have grade > 3" → 拒绝。
    """
    if not _has_agg_word(question, "COUNT", is_zh):
        return False
    if is_zh:
        m = _ZH_BARE_LABEL_RE.match(hit.question)
        label = m.group(1) if m else (hit.tags[0] if hit.tags else table)
        return label in question
    if _YEAR_RE.search(question):
        return False  # "in 1997" → date_range 家族接手
    leftover = _tokens(question) - _STRUCT_EN - {_norm_token(table.lower())}
    return not leftover


def _match_enum_filter(hit: ExampleHit, question: str, table: str, is_zh: bool) -> bool:
    """枚举过滤 COUNT(`WHERE col = 'code'`):模板措辞的 content token 必须在问题里。

    强制 label 词("male"/"female")出现——"how many clients are male?"
    命中,"how many clients are there?" 不命中。zh 枚举模板的 code 值
    无法从自然措辞可靠匹配,一律 miss(保守)。
    """
    if is_zh:
        return False
    if not _has_agg_word(question, "COUNT", False):
        return False
    required = (
        _tokens(_strip_agg_phrases(hit.question))
        - _STRUCT_EN - {_norm_token(table.lower())}
    )
    if not required:
        return False
    return required <= (_tokens(question) - _STRUCT_EN)


def _match_aggregate(hit: ExampleHit, question: str, table: str, agg: str,
                     is_zh: bool) -> bool:
    """聚合模板(MAX/MIN/AVG/SUM,含日期 earliest/latest):意图词 + desc 锚定。"""
    if not _has_agg_word(question, agg, is_zh):
        return False
    if is_zh:
        m = _ZH_AGG_DESC_RE.search(hit.question)
        desc = m.group(1) or m.group(2) or ""
        label = hit.tags[0] if hit.tags else table
        return bool(desc and desc in question and label in question)
    # 列描述锚定:共享 token/前缀(至少一个 desc 词出现)。子集检查会把
    # 用户自然措辞("approved")挡在列名模板("approved_date")外;
    # 聚合词 + 表锚 + desc 词至少一个出现已是足够的防错配。
    required = (
        _tokens(_strip_agg_phrases(hit.question))
        - _STRUCT_EN - {_norm_token(table.lower())}
    )
    return _desc_overlap(required, _tokens(question))


def _match_date_range(hit: ExampleHit, question: str, table: str, is_zh: bool) -> bool:
    """日期区间模板(in/between/on/before/after):计数词 + 字面量 + 方向词。

    模板里的全部年份必须出现在问题里("between 1995 and 1997" 需要两个
    年份都在;"in 1997" 不命中 between 模板);on-date 模板的完整日期
    字面量同样要求;before/after 模板另需方向词。
    """
    if not _has_agg_word(question, "COUNT", is_zh):
        return False
    if is_zh:
        m = _ZH_DATE_DESC_RE.search(hit.question)
        desc = m.group(1) if m else ""
        label = hit.tags[0] if hit.tags else table
        if not desc or desc not in question or label not in question:
            return False
    else:
        required = (
            _tokens(_strip_agg_phrases(hit.question))
            - _STRUCT_EN - _DATE_STRUCT_EN - {_norm_token(table.lower())}
        )
        # 列描述锚定:共享 token/前缀,不含数字 token(年份字面量另有专项检查)
        required = {t for t in required if not re.search(r"\d", t)}
        if not _desc_overlap(required, _tokens(question)):
            return False
    # 用户带区间词时,纯 in 模板不得抢答(between/before/after/on 有专项模板)
    tpl_interval = bool(re.search(r"\b(between|before|after|on)\b", hit.question, re.I))
    q_interval = bool(re.search(r"\b(between|before|after|on)\b", question, re.I))
    if not tpl_interval and q_interval:
        return False
    # 字面量检查(两种语言同一套:模板里的数字必须出现在问题里)
    tpl_years = set(_YEAR_RE.findall(hit.question))
    if tpl_years:
        q_years = set(_YEAR_RE.findall(question))
        if not tpl_years <= q_years:
            return False
    tpl_dates = set(_DATE_LITERAL_RE.findall(hit.question))
    if tpl_dates:
        if not tpl_dates <= set(_DATE_LITERAL_RE.findall(question)):
            return False
    # 方向词
    if re.search(r"\bbefore\b", hit.question, re.I) and not re.search(
            r"\b(before|prior to|earlier than|之前|以前)\b", question, re.I):
        return False
    if re.search(r"\bafter\b", hit.question, re.I) and not re.search(
            r"\b(after|later than|since|之后|以后)\b", question, re.I):
        return False
    return True


def match_fast_template(
    question: str,
    hits: list[ExampleHit],
    matched_tables: list[str],
    *,
    max_len: int = FAST_PATH_MAX_QUESTION_LEN,
) -> dict[str, Any] | None:
    """模板匹配:第一个全过四重约束的模板胜出,否则 None(走正常链路)。"""
    q = (question or "").strip()
    if not q or len(q) > max_len or not matched_tables:
        return None
    matched_lower = {t.lower() for t in matched_tables if t}
    is_zh = bool(_CJK_RE.search(q))
    for hit in hits:
        if not hit.template or not hit.sql:
            continue
        ok, table, agg, has_where = template_sql_shape_ok(hit.sql)
        if not ok:
            continue
        if not _table_anchored(table, q, matched_lower):
            continue
        if hit.date_range:
            matched = _match_date_range(hit, q, table, is_zh)
        elif hit.aggregate or agg in ("MAX", "MIN", "AVG", "SUM"):
            matched = _match_aggregate(hit, q, table, agg, is_zh)
        elif has_where:
            matched = _match_enum_filter(hit, q, table, is_zh)
        else:
            matched = _match_bare_count(hit, q, table, is_zh)
        if matched:
            return {
                "sql": hit.sql,
                "question": hit.question,
                "tags": list(hit.tags),
            }
    return None


def make_fast_match(
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
    config: AgentConfig | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the fast-match node: hit → inject template SQL, skip the LLM path.

    Miss (or any gate) → empty update: the pipeline continues to the planner
    unchanged. Correction rounds never fast-path (KB standard SQL already
    failed; templates are the same deterministic family).
    """

    async def fast_match(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        cfg = config or AgentConfig()
        if not cfg.fast_path:
            return {}
        if state.intent != "query":
            return {}
        # 修正轮(error_feedback/error_analysis/reason 任一)不快径;也覆盖
        # 回滚到 schema_linking 后的重入(error_feedback 已置位)
        if bool(state.error_feedback or state.error_analysis or state.reason):
            return {}
        if kb is None or connectors is None or not connectors.default_name:
            return {}
        datasource = connectors.default_name
        try:
            await kb.ensure_synced(default_datasource=datasource)
            hits = await kb.list_templates(datasource)
        except Exception as exc:  # KB 失败绝不停管线:静默 miss
            logger.warning("fast_match KB failure: %s", exc)
            return {}
        if not hits:
            return {}
        dialect = state.dialect
        try:
            adapter = await connectors.get()
            dialect = adapter.dialect()
        except Exception:
            pass
        m = match_fast_template(state.question, hits, list(state.matched_tables or []))
        if m is None:
            return {}
        # 模板快径命中进 langfuse(无 Langfuse 时 no-op)
        with record_span(
            "kb.template_hit",
            input={"question": state.question, "template": m["question"], "tags": m["tags"]},
        ) as span:
            if span is not None:
                span.update(output={"sql": m["sql"]})
        return {
            "sql": m["sql"],
            "dialect": dialect,
            "fast_path": True,
            "complexity": "simple",
            "kb_hits": [{
                "kind": "template",
                "question": m["question"],
                "sql": m["sql"],
                "tags": m["tags"],
                "source": "fast_path",
            }],
        }

    return fast_match
