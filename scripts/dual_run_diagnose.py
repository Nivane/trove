#!/usr/bin/env python3
"""双跑诊断:快径 vs 语义编译路径的不一致率(离线,零 LLM,默认不在任何 CI)。

设计稿 ``2026-09-28-verified-query-assets-design.md`` §8.5 / P6。

**为什么要双跑**:``fast_match`` 快径(certified 模板)与语义编译路径在语法上
互斥 —— 快径命中就不走编译。于是「这两条路会不会给出不同的答案」这件事,
在线上**没有一次运行能同时看到两侧**。本脚本在快径命中时额外跑一次编译并
比对,把「不一致率」这个评估指标算出来。它是评估基建,不是运行时功能:成本
敏感,默认关,不进主链路。

**为什么能离线**:两侧都不需要 LLM。

- 快径侧原样调用 ``match_fast_template``(生产同一个纯函数);
- 编译侧调 ``SemanticCompiler.compile_detailed``(生产同一个编译器),喂进去
  的 plan 由**问题 + 声明模型**确定性合成 —— 聚合意图取问题里的聚合词,
  度量按「数据集命中 + 聚合函数相同 + 列/名称有证据」唯一选出,过滤条件由
  模型声明的 ``enum_display``/``value_aliases`` 词表把问题里的**人类标签**
  映回存储 code。线上这份 plan 是 LLM 产的;这里是确定性代餐,差别在
  「能不能解析出唯一计划」,不在 SQL 语义。

**已知偏差(读数字前必看)**:

1. ``matched_tables`` 来自语料自身的 SQL(gold/template)而不是 schema_linking
   的产出 —— 这是**有意**的:隔离「检索召回」变量,只比 SQL 语义。代价是
   快径命中率高于线上(线上拿不到这些表就不会命中)。
2. 过滤器只认**声明词表里的标签**。问题用 code 本身说话("are B?")、或带
   年份/数值区间时,诊断侧合成不出条件 → 整题落进 ``filter_unresolved``,
   **不进分母**(宁可少一条数据,不肯多一条假不一致)。
3. 合成不出唯一计划(多指标同样贴合/多标签同时命中)一律不猜,落进
   ``ambiguous_*`` 桶。所以分母只覆盖「模型能确定性表达该问题」的那部分题;
   分母之外的数量在报告里单列,读率时必须连着看。
4. 快径侧的 RLS 注入被复现(否则声明了 ``row_filter`` 的模型会凭空多出一堆
   假不一致);注入失败按线上行为判 miss。

**口径**:一致 = 两侧 SQL 的**结果形状签名**相等 —— 复用生产守卫
``compiled_sql_matches`` 的判据(``PlanSignature.matches``:投影/聚合/表/
过滤列与值/联表数/分组宽度),所以大小写、空白、别名、裸列 vs 限定列、
``COUNT(*)`` vs ``COUNT(col)`` 都算一致。生产签名**不看**的四项(ORDER BY、
GROUP BY **列**、LIMIT、DISTINCT)由本脚本补一层尾项守卫 —— 快径模板的形状
守卫本就排除 ORDER BY/GROUP BY,编译侧多给一个就是偏离,不该静默放行。

**假阳性风险**(会把等价的判成不一致):过滤条件顺序不同(签名按 WHERE 遍历
顺序逐项 zip 比对)、编译器产出的时间分桶表达式与模板措辞不同、模板与模型
对"同一个问题"的理解确实不同(这一条是真阳性,只是未必是 bug)。

**假阴性风险**(会把不一致的判成一致):分区/窗口函数、``GROUP BY`` 列在两侧
都写成表达式而非裸列、语义等价但形状签名覆盖不到的重写(如 ``SUM(a)`` vs
``SUM(b) - SUM(c)`` 恰好同形 —— 实际不会同形,列出以示边界)。

用法::

  # 默认语料 = <kb>/<datasource>/examples.yml
  uv run python scripts/dual_run_diagnose.py --datasource financial

  # 加 BIRD 基线 32 题(自带 gold_sql,用于取锚定表),只看不一致明细
  uv run python scripts/dual_run_diagnose.py --datasource financial \
      --corpus eval/baseline/questions.jsonl --only-disagree

  # 落 JSON 供后续对比;--fail-over 默认关(本仓库不接 CI)
  uv run python scripts/dual_run_diagnose.py --datasource financial --json /tmp/dual.json

退出码:0 正常;1 ``--fail-over`` 被触发;2 语料/模型/参数有问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from trove.services.kb.service import ExampleHit, KbService
from trove.services.semantic_layer import rls
from trove.services.semantic_layer.compiler import (
    CompileResult,
    SemanticCompiler,
    _generated_signature,
)
from trove.services.semantic_layer.models import (
    SemanticField,
    SemanticMetric,
    SemanticModel,
)
from trove.workflow.nodes.fast_match import (
    _CJK_RE,
    _has_agg_word,
    match_fast_template,
)

# 故意复用 fast_match 的私有意图词表(``_has_agg_word``/``_CJK_RE``):双跑的
# 两侧必须由**同一份**聚合意图驱动。另写一套词表 = 迟早与快径的判据漂移,
# 那时诊断报出的「不一致」里会混进「我读错了意图」这种诊断自身的错,而那种
# 噪声最难查(它看起来像产品缺陷)。代价是耦合了同模块的两个私有名 ——
# 快径改词表时这个脚本需要跟着看一眼,这是值得的。

# ── 状态:进分母的只有前两个,其余都是「不可比」 ──────────────

AGREE = "agree"
DISAGREE = "disagree"
UNDECIDABLE = "undecidable"
FAST_MISSED = "fast_missed"
COMPILE_MISSED = "compile_missed"
NO_DECLARED_METRIC = "no_declared_metric"
AMBIGUOUS_METRIC = "ambiguous_metric"
AMBIGUOUS_LABEL = "ambiguous_label"
FILTER_UNRESOLVED = "filter_unresolved"

_REASON_TEXT = {
    UNDECIDABLE: "至少一侧 SQL 解析不出查询形状 —— 判不了,不算一致",
    FAST_MISSED: "快径 miss(平凡单表聚合之外的问题,或没有锚定表)—— 未跑编译",
    COMPILE_MISSED: "快径命中但声明模型编译不出权威 SQL(硬 MISS 或软骨架)",
    NO_DECLARED_METRIC: "声明模型里没有能度量该问题的聚合指标",
    AMBIGUOUS_METRIC: "多个声明指标同样贴合问题 —— 歧义不猜",
    AMBIGUOUS_LABEL: "同一字段有多个声明词表值同时命中问题 —— 值歧义不猜",
    FILTER_UNRESOLVED: "快径 SQL 过滤在声明词表表达不了的列上 —— 合成不出对侧条件",
}

# 词元比对用停用词:只去「问题句式 + 聚合意图」两类词,不动领域词。
# 聚合词必须去 —— 意图已由 aggregate_intent 单独判定,留在词集里会让
# 「total Loan amount」与「average Loan amount」两个指标变得难以区分。
_STOPWORDS = frozenset("""
a an the of for in on with to and or per by is are was were be been being
what which who how many much does do did there here that this these those
it its me my i you we our their all any list show give get find
record records row rows table tables
number count total sum average avg mean max min maximum minimum
latest newest earliest oldest highest lowest largest smallest
value values
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# 聚合意图的判定顺序:COUNT 先于 SUM —— "how many ... total" 这类句子里
# 两个词都在,取更外层的意图(计数)才符合问句语义。
_AGG_ORDER = ("COUNT", "AVG", "SUM", "MAX", "MIN")


def _norm_token(token: str) -> str:
    """复数归一(仅末尾 s)—— 与 fast_match 同款朴素处理。"""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def tokens(text: str) -> set[str]:
    """小写词元、去停用词、复数归一、下划线也分词(applied_date → applied,date)。"""
    return {_norm_token(t) for t in _TOKEN_RE.findall((text or "").lower())} - _STOPWORDS


def aggregate_intent(question: str) -> str:
    """问题要求的聚合函数(COUNT/AVG/SUM/MAX/MIN),读不出 → ""。

    读的是**问题**不是 SQL:两侧必须由同一份意图驱动,否则「不一致」里
    会混进「我拿错了意图」这种诊断自身的错。
    """
    q = question or ""
    is_zh = bool(_CJK_RE.search(q))
    for fn in _AGG_ORDER:
        if _has_agg_word(q, fn, is_zh):
            return fn
    return ""


def tables_in(sql: str) -> list[str]:
    """SQL 引用的表名(小写、去重、升序);解析不出 → 空。"""
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:
        return []
    return sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name})


def where_columns(sql: str) -> set[str]:
    """顶层 WHERE 引用的列名(小写);无 WHERE/解析不出 → 空。"""
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:
        return set()
    where = tree.args.get("where")
    if where is None:
        return set()
    return {c.name.lower() for c in where.find_all(exp.Column) if c.name}


# ── 口径:两条 SQL 算不算一致 ─────────────────────────────


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str = ""


def _tail_shape(sql: str, dialect: str) -> tuple:
    """生产签名**不看**的四项:去重、分组列、排序、限行。

    ``PlanSignature`` 只比分组**个数**,ORDER BY / LIMIT 完全不在其内 ——
    那是为「LLM 补排序」有意留的宽松。快径模板的形状守卫本就排除 ORDER BY
    与 GROUP BY,所以对双跑而言「编译侧多出排序/限行」是偏离而非等价改写,
    这一层就是补它。``COUNT(DISTINCT x)`` 同理:生产签名里聚合函数名取
    ``agg.sql().split("(")[0]``,DISTINCT 被吃掉,``COUNT(x)`` 与
    ``COUNT(DISTINCT x)`` 会同签名 —— 这里按投影逐个补一个去重标志。
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return ()
    node = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if node is None:
        return ()
    distinct = (
        bool(node.args.get("distinct")),
        tuple(bool(e.find(exp.Distinct)) for e in (node.expressions or [])),
    )
    group = node.args.get("group")
    groups = tuple(sorted(
        tuple(sorted(c.name.lower() for c in e.find_all(exp.Column) if c.name))
        for e in (group.expressions if group is not None else [])
    ))
    order = node.args.get("order")
    ordering = tuple(
        (tuple(sorted(c.name.lower() for c in o.find_all(exp.Column) if c.name)),
         bool(o.args.get("desc")))
        for o in (order.expressions if order is not None else [])
    )
    limit = node.args.get("limit")
    lim = str(limit.expression.sql()) if limit is not None and limit.expression else ""
    return (distinct, groups, ordering, lim)


def _shape_diff(a: Any, b: Any) -> str:
    """签名不等 → 指出第一个不同的维度(给人看的复核线索,不参与判定)。"""
    for name in ("projections", "tables", "conds", "joins", "groups"):
        left, right = getattr(a, name), getattr(b, name)
        if left != right:
            return f"结果形状不同:{name} {left!r} vs {right!r}"
    return "结果形状不同(宽容比对下仍不等)"


def equivalent_sql(fast_sql: str, compiled_sql: str, dialect: str = "sqlite") -> Verdict:
    """双跑判据:两侧 SQL 的结果形状是否等价。

    解析不出的一侧 → ``undecidable``,**绝不**因为「两侧都解析不出」就判一致
    （那会把「判不了」记成「一致」,指标朝好看的方向错)。判据同生产守卫
    ``compiled_sql_matches``,外加尾项守卫,见 ``_tail_shape``。
    """
    left = _generated_signature(fast_sql, dialect)
    right = _generated_signature(compiled_sql, dialect)
    if left is None or right is None:
        return Verdict(UNDECIDABLE, _REASON_TEXT[UNDECIDABLE])
    if not left.matches(right):
        return Verdict(DISAGREE, _shape_diff(left, right))
    tail_l, tail_r = _tail_shape(fast_sql, dialect), _tail_shape(compiled_sql, dialect)
    if tail_l != tail_r:
        return Verdict(DISAGREE, f"尾项不同(去重/分组列/排序/限行): {tail_l} vs {tail_r}")
    return Verdict(AGREE)


# ── 计划合成:问题 + 声明模型 → 唯一计划,或明确说不出来 ─────


def _first_agg(expression: str) -> str:
    """表达式里第一个聚合函数的函数名(小写);无聚合 → ""。"""
    try:
        tree = sqlglot.parse_one(expression or "")
    except Exception:
        return ""
    agg = next(iter(tree.find_all(exp.AggFunc)), None)
    return agg.sql().split("(", 1)[0].strip().lower() if agg is not None else ""


def _expr_columns(expression: str) -> set[str]:
    """表达式引用的列名(小写);解析不出 → 空。"""
    try:
        tree = sqlglot.parse_one(expression or "")
    except Exception:
        return set()
    return {c.name.lower() for c in tree.find_all(exp.Column) if c.name}


def _is_identifier(f: SemanticField) -> bool:
    """标识符字段:主键/外键列。它们不承载「在问什么」的信息。"""
    return f.semantic_role == "identifier" or f.name.lower().endswith("_id")


def _family_tokens(datasets: list[str], model: SemanticModel) -> set[str]:
    """锚定数据集里**非标识符**字段的词集 —— 问题点到这些词就是点了该表的某个属性。"""
    by_name = {d.name: d for d in model.datasets}
    out: set[str] = set()
    for name in datasets:
        ds = by_name.get(name)
        if ds is None:
            continue
        for f in ds.fields:
            if not _is_identifier(f):
                out |= tokens(f.name)
    return out


def resolve_metric(
    question: str, datasets: list[str], model: SemanticModel | None,
) -> tuple[SemanticMetric | None, str]:
    """问题 → 唯一声明度量;说不出来 → ``(None, 状态 slug)``。

    候选必须同时满足四关,缺一不可(每一关都是为了让「猜错」变成「不猜」):

    1. 与锚定数据集有交集,且首个聚合函数与问题意图**同名**;
    2. 有证据:表达式的列词 或 名称/别名词(≥2 个)出现在问题里;
    3. **不忽略问题点到的已声明字段** —— 问题点了 ``payment`` 而候选指标只
       度量 ``amount``,说明它答的不是这个问题。原型上这一关拦掉过一次真实
       误判:``maximum Monthly payment amount`` 被解析成 ``max_loan_amount``
       (唯一 MAX 指标 + 共享 "amount" 词),凭空造出一条假不一致;
    4. 得分唯一最大。并列即歧义,不猜。
    """
    if model is None:
        return None, NO_DECLARED_METRIC
    intent = aggregate_intent(question)
    if not intent:
        return None, NO_DECLARED_METRIC
    q = tokens(question)
    family = _family_tokens(datasets, model)
    anchor = set(datasets)
    scored: list[tuple[int, SemanticMetric]] = []
    for m in model.metrics:
        if not (set(m.datasets) & anchor) or _first_agg(m.expression) != intent.lower():
            continue
        cols = tokens(" ".join(_expr_columns(m.expression)))
        names = tokens(m.name) | tokens(" ".join(m.synonyms))
        if not (cols & q or len(names & q) >= 2):
            continue
        if (q & family) - cols:
            continue
        scored.append((len(q & (names | cols)), m))
    if not scored:
        return None, NO_DECLARED_METRIC
    best = max(s for s, _ in scored)
    top = [m for s, m in scored if s == best]
    if len(top) != 1:
        return None, AMBIGUOUS_METRIC
    return top[0], ""


def _declared_labels(f: SemanticField) -> dict[str, list[str]]:
    """字段声明的 ``code → [人类标签]``(enum_display 主标签 + value_aliases 别名)。"""
    labels: dict[str, list[str]] = {}
    for code, label in (f.enum_display or {}).items():
        if str(label).strip():
            labels.setdefault(str(code), []).append(str(label))
    for code, aliases in (f.value_aliases or {}).items():
        for alias in aliases or []:
            if str(alias).strip():
                labels.setdefault(str(code), []).append(str(alias))
    return labels


def declared_conditions(
    question: str, datasets: list[str], model: SemanticModel | None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """问题里的**人类标签** → 声明过滤条件;(None, slug) 表示值歧义不猜。

    值的归一交给编译器(``_enum_code_for``):这里递上去的是问题里的原词
    ("bad"),编译期用同一份声明把它映成存储 code("B")。诊断脚本自带一份
    归一表就等于把「模型声明了什么」抄了一遍 —— 那会让诊断失去意义。

    只认标签不认 code:单字母 code("B")在自然语言里到处撞词,按 code 匹配
    会凭空造出条件。这一限制的代价是这类整题落进 ``filter_unresolved``。
    """
    if model is None:
        return None, AMBIGUOUS_LABEL
    low = (question or "").lower()
    by_name = {d.name: d for d in model.datasets}
    out: list[dict[str, Any]] = []
    for name in datasets:
        ds = by_name.get(name)
        if ds is None:
            continue
        for f in ds.fields:
            labels = _declared_labels(f)
            if not labels:
                continue
            hits = {
                code: [
                    lab for lab in labs
                    if re.search(rf"\b{re.escape(lab.lower())}\b", low)
                ]
                for code, labs in labels.items()
            }
            hits = {code: found for code, found in hits.items() if found}
            if len(hits) > 1:
                return None, AMBIGUOUS_LABEL
            if hits:
                code, found = next(iter(hits.items()))
                out.append({"field": f"{name}.{f.name}", "op": "=", "value": found[0]})
    return out, ""


# ── 单题双跑 + 报告 ────────────────────────────────────


@dataclass
class Row:
    """一题的双跑结果。``fast_sql``/``compiled_sql`` 是复核凭据,不是日志。"""

    question: str
    status: str
    reason: str = ""
    fast_sql: str = ""
    compiled_sql: str = ""
    metric: str = ""

    @property
    def comparable(self) -> bool:
        """只有「一致」「不一致」进分母;其余一律不可比。"""
        return self.status in (AGREE, DISAGREE)


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    corpus: str = ""
    datasource: str = ""
    dialect: str = "sqlite"
    command: str = ""

    @property
    def n_agree(self) -> int:
        return sum(1 for r in self.rows if r.status == AGREE)

    @property
    def n_disagree(self) -> int:
        return sum(1 for r in self.rows if r.status == DISAGREE)

    @property
    def n_comparable(self) -> int:
        return self.n_agree + self.n_disagree

    @property
    def rate(self) -> float | None:
        """不一致率;分母为 0 → ``None``。

        不是 0.0:0/0 与「一条都不一致」是两件事,前者只能说明这次没测到
        任何可比样本。返回 None 让调用方必须显式处理,而不是打印一个漂亮
        的 0.0% 让人以为覆盖到了。
        """
        return self.n_disagree / self.n_comparable if self.n_comparable else None

    def excluded(self) -> dict[str, int]:
        """分母之外的题按原因计数 —— 读率之前必须连着看这张表。"""
        out: dict[str, int] = {}
        for r in self.rows:
            if not r.comparable:
                out[r.status] = out.get(r.status, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "corpus": self.corpus,
            "datasource": self.datasource,
            "dialect": self.dialect,
            "command": self.command,
            "n_total": len(self.rows),
            "n_comparable": self.n_comparable,
            "n_agree": self.n_agree,
            "n_disagree": self.n_disagree,
            "disagreement_rate": self.rate,
            "excluded": self.excluded(),
            "rows": [
                {"question": r.question, "status": r.status, "reason": r.reason,
                 "metric": r.metric, "fast_sql": r.fast_sql,
                 "compiled_sql": r.compiled_sql}
                for r in self.rows
            ],
        }

    def render(self, *, only_disagree: bool = False) -> list[str]:
        """率 + 明细 + 命令行:光秃秃的百分比不可复核。"""
        lines = [
            "双跑诊断:快径(模板) vs 语义编译路径 —— 离线,零 LLM",
            f"语料: {self.corpus} | 数据源: {self.datasource} | 方言: {self.dialect}",
            f"命令行: {self.command}",
            "",
        ]
        if self.n_comparable:
            lines.append(
                f"可比 {self.n_comparable} 题: 一致 {self.n_agree} / 不一致 "
                f"{self.n_disagree} → 不一致率 {self.n_disagree}/{self.n_comparable} "
                f"({self.rate * 100:.1f}%)")
        else:
            lines.append(
                "可比 0 题 → 不一致率 n/a(两条路径没有一条题同时产出 SQL,"
                "这个 n/a 说明**没测到**,不说明没分歧)")
        excluded = self.excluded()
        lines.append(
            "不计入分母: " + (" · ".join(
                f"{status} {n}" for status, n in sorted(
                    excluded.items(), key=lambda kv: -kv[1]))
                if excluded else "无"))
        lines += ["", "--- 明细 ---"]
        shown = 0
        for r in self.rows:
            if only_disagree and r.status != DISAGREE:
                continue
            shown += 1
            lines.append(f"[{r.status}] {r.question}")
            if r.reason:
                lines.append(f"    原因    : {r.reason}")
            if r.metric:
                lines.append(f"    声明指标: {r.metric}")
            if r.fast_sql:
                lines.append(f"    快径    : {r.fast_sql}")
            if r.compiled_sql:
                lines.append(f"    编译    : {r.compiled_sql}")
        if not shown:
            lines.append("(无)")
        return lines


def summarize(
    rows: list[Row], *, corpus: str, datasource: str, dialect: str, command: str,
) -> Report:
    return Report(rows=rows, corpus=corpus, datasource=datasource,
                  dialect=dialect, command=command)


def diagnose_row(
    *,
    question: str,
    matched_tables: list[str],
    hits: list[ExampleHit],
    model: SemanticModel | None,
    dialect: str = "sqlite",
) -> Row:
    """单题双跑:先快径,命中才额外跑一次编译,再比对。

    顺序是刻意的 —— 只有快径命中才付编译成本(这正是 §8.5 说的额外成本)。
    快径 miss 的题在这里零编译开销,和线上一致。
    """
    matched = [t for t in (matched_tables or []) if t]
    hit = match_fast_template(question, hits, matched)
    if hit is None:
        return Row(question=question, status=FAST_MISSED,
                   reason=_REASON_TEXT[FAST_MISSED])
    fast_sql = str(hit["sql"])

    metric, why = resolve_metric(question, matched, model)
    if metric is None:
        return Row(question=question, status=why, reason=_REASON_TEXT[why],
                   fast_sql=fast_sql)
    conditions, why = declared_conditions(question, matched, model)
    if conditions is None:
        return Row(question=question, status=why, reason=_REASON_TEXT[why],
                   fast_sql=fast_sql, metric=metric.name)

    # 快径 SQL 过滤在声明词表表达不了的列上 → 对侧必然少一个条件,那条
    # 「不一致」是诊断侧的合成失败,不是快径的错。拦在这里而不是事后补救。
    unexplained = where_columns(fast_sql) - {
        str(c["field"]).rsplit(".", 1)[-1] for c in conditions}
    if unexplained:
        return Row(question=question, status=FILTER_UNRESOLVED,
                   reason=f"{_REASON_TEXT[FILTER_UNRESOLVED]}:{sorted(unexplained)}",
                   fast_sql=fast_sql, metric=metric.name)

    # 快径侧复现线上 RLS 注入:声明了 row_filter 的模型下,快径自己注入
    # (fast_match 的职责),不注入就会拿「无过滤的快径」比「有过滤的编译」,
    # 每条题都是假不一致。注入失败 = 线上快径也会放弃 → 判 miss。
    if rls.declared_rls(model):
        try:
            fast_sql = rls.inject_row_filters(fast_sql, model, dialect)
        except rls.RLSInjectionError as exc:
            return Row(question=question, status=FAST_MISSED,
                       reason=f"快径 RLS 注入失败(线上同样放弃快径):{exc}",
                       fast_sql=fast_sql, metric=metric.name)

    plan = {
        "tables": list(matched),
        "aggregation": metric.name,
        "answer_columns": [metric.name],
        "conditions": conditions,
    }
    result = SemanticCompiler(model).compile_detailed(
        plan, list(matched), force_dialect=dialect)
    if not isinstance(result, CompileResult):
        detail = getattr(result, "reason", "") or "; ".join(
            f"{p.get('reason')}:{p.get('component')}"
            for p in (getattr(result, "miss_parts", None) or []))
        return Row(question=question, status=COMPILE_MISSED,
                   reason=f"{_REASON_TEXT[COMPILE_MISSED]}({detail})",
                   fast_sql=fast_sql, metric=metric.name)

    verdict = equivalent_sql(fast_sql, result.sql, dialect)
    return Row(question=question, status=verdict.status, reason=verdict.reason,
               fast_sql=fast_sql, compiled_sql=result.sql, metric=metric.name)


# ── IO 层(薄):读语料、取 KB 与语义模型、打印 ─────────────


@dataclass(frozen=True)
class CorpusItem:
    question: str
    matched_tables: list[str]


def load_corpus(path: Path) -> list[CorpusItem]:
    """按后缀读语料,锚定表一律取自语料自身的 SQL(见模块 docstring 偏差 1)。

    - ``.yml/.yaml``:KB examples(``examples:`` 列表,每条带 ``question``/``sql``);
    - ``.jsonl``:每行 ``question`` + ``gold_sql``(或 ``sql``);
    - 其它(txt):每行一个问题,**没有 SQL 可取锚定表** → 锚定为空,
      这类题会在快径那一步 miss(诚实:无锚定就是不命中)。
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yml", ".yaml"):
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return []
        return [
            CorpusItem(question=str(e.get("question") or "").strip(),
                       matched_tables=tables_in(str(e.get("sql") or "")))
            for e in (data.get("examples") or [])
            if isinstance(e, dict) and str(e.get("question") or "").strip()
        ]
    if path.suffix == ".jsonl":
        out: list[CorpusItem] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # 容忍半行/坏行(评测中断的常见残留)
            question = str(entry.get("question") or "").strip()
            if not question:
                continue
            sql = str(entry.get("gold_sql") or entry.get("sql") or "")
            out.append(CorpusItem(question=question, matched_tables=tables_in(sql)))
        return out
    return [
        CorpusItem(question=ln.strip(), matched_tables=[])
        for ln in text.splitlines() if ln.strip()
    ]


def load_semantic_model(
    kb: KbService, datasource: str, dialect: str,
) -> SemanticModel | None:
    """该数据源的语义模型(离线:只读 OSSIE YAML,零网络零 LLM)。"""
    from trove.services.semantic_layer.provider import SemanticLayerProvider

    provider = SemanticLayerProvider(
        directory=Path.cwd() / ".trove" / "semantic" / datasource,
        datasource=datasource,
        dialect=dialect,
        kb_semantics_path=kb.semantics_path(datasource),
    )
    if not provider.enabled:
        return None
    return provider.model()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="双跑诊断:快径 vs 语义编译路径的不一致率(离线,零 LLM)")
    p.add_argument("--datasource", default="financial",
                   help="数据源/KB 名(决定默认语料与语义模型,默认 financial)")
    p.add_argument("--corpus", action="append", default=[],
                   help="语料文件,可重复;默认 <kb>/<datasource>/examples.yml")
    p.add_argument("--dialect", default="sqlite", help="编译/比对方言(默认 sqlite)")
    p.add_argument("--kb-dir", default=None, help="KB 根目录(默认 <cwd>/.trove/kb)")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 题(0=全部)")
    p.add_argument("--only-disagree", action="store_true", help="明细只打印不一致的题")
    p.add_argument("--json", default="", help="把报告写成 JSON(率 + 逐题明细)")
    # 默认关:本仓库不把它接进 CI —— 它需要 KB 语料与语义模型在场,
    # 不是每次提交都该跑的东西(设计 §8.5:评估基建,不是运行功能)。
    p.add_argument("--fail-over", type=float, default=None,
                   help="不一致率超过该值时退出码 1(默认关)")
    p.add_argument("--min-comparable", type=int, default=10,
                   help="--fail-over 生效所需的最小分母(默认 10,防小样本误判)")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    kb = KbService(Path.cwd(), kb_dir=args.kb_dir)
    await kb.ensure_synced(args.datasource)
    hits = await kb.list_templates(args.datasource)
    model = load_semantic_model(kb, args.datasource, args.dialect)
    if model is None:
        print(f"no semantic model for {args.datasource}: "
              f"{kb.semantics_path(args.datasource)} 不存在或读不出来",
              file=sys.stderr)
        return 2

    corpora = [Path(c) for c in args.corpus] or [
        kb.kb_dir / args.datasource / "examples.yml"]
    items: list[CorpusItem] = []
    for path in corpora:
        if not path.exists():
            print(f"corpus not found: {path}", file=sys.stderr)
            return 2
        items += load_corpus(path)
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("no questions in corpus", file=sys.stderr)
        return 2

    rows = [
        diagnose_row(question=i.question, matched_tables=i.matched_tables,
                     hits=hits, model=model, dialect=args.dialect)
        for i in items
    ]
    report = summarize(
        rows,
        corpus=" + ".join(str(p) for p in corpora),
        datasource=args.datasource,
        dialect=args.dialect,
        command=" ".join(sys.argv),
    )
    print("\n".join(report.render(only_disagree=args.only_disagree)))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\njson → {args.json}")
    if (args.fail_over is not None and report.rate is not None
            and report.n_comparable >= args.min_comparable
            and report.rate > args.fail_over):
        print(f"不一致率 {report.rate:.4f} > --fail-over {args.fail_over}",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
