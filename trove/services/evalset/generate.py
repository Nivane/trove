"""semantic 冷启动生成:确定性候选枚举 + 编译/试执行护栏 + 可选 LLM 改写。

本模块只**产出候选**(``GeneratedItem``):不落盘、不去重、不碰确认门 ——
合并进 ``evalset.yml``、去重与泄漏标记由 Task 7 的编排负责。

生成路径刻意是「先编译、后试执行」两道确定性护栏:

1. 每个候选先过语义编译器的声明式 API(``build_and_compile``)——未声明
   的组件抛 ``SemanticQueryError``,候选被跳过并计入 ``compile_miss``;
2. 编译产物再过一次有界试执行(``LIMIT 1``)——只有真能在目标库跑起来的
   SQL 才会成为参考 SQL。``registry`` 缺失且调用方给了数据源时(测试/无
   连接场景)由调用方保证;数据源缺失则无法试执行,按 ``trial_failed``
   跳过而不是放行一条没验证过的 SQL。

LLM 改写是可选装饰(``rewrite=True``):一次调用改写全部问句,解析失败 /
长度不符时**保留模板问句**并记 ``rewrite_failed`` —— 改写出错不能让整批
候选消失。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.prompts import render
from trove.services.evalset.models import MAX_GENERATE_PER_RUN
from trove.services.evalset.templates import (
    CANDIDATE_GRAINS,
    GRAINS,
    Candidate,
    format_question,
    natural_name,
)
from trove.services.kb.sql_guard import sqlglot_ok, trial_ok
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)
from trove.services.semantic_layer.query import (
    SemanticQuery,
    SemanticQueryError,
    build_and_compile,
)

logger = get_logger(__name__)

#: 条目来源 / 参考 SQL 来源 / 生成器标识(全部落在 evalset 的闭集里)。
SOURCE = "semantic"
ORIGIN = "semantic_compiler"
GENERATOR = "evalset.semantic.v1"

#: 枚举去重排序的默认盐(换盐 = 换一批候选的优先顺序,但仍确定性)。
DEFAULT_SALT = "evalset.semantic.v1"

#: top_n 候选的条数(问题文本与查询 limit 同源)。
TOP_N = 5

#: 每个字段只取前 N 个枚举值做 filter 候选(值域可能上百,前两个已是
#: 代表性抽样;更多值只会挤占候选预算)。
MAX_VALUES_PER_FIELD = 2

_REWRITE_MAX_TOKENS = 800
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass
class GeneratedItem:
    """一个源产出的候选条目(Task 7 用它构造 ``EvalsetItem``)。

    刻意**没有** ``qid`` / ``status`` / ``content_hash``:那些是落盘时的
    身份与状态,由编排层统一分配(候选之间不能各自发明 id 空间)。
    """

    question: str
    reference_sql: str
    source: str
    reference_origin: str
    generator: str
    tags: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    needs_reference: bool = False
    created_by: str = ""


@dataclass
class SourceResult:
    """一个生成源的结果:产物 + 跳过计数 + 降级说明 + 状态。"""

    items: list[GeneratedItem] = field(default_factory=list)
    skipped: int = 0
    degraded: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"


@dataclass
class GenerateContext:
    """一次生成的共享上下文(数据源 / 语言 / 模型 / 连接与身份)。"""

    datasource: str | None
    lang: str
    dialect: str
    llm: Any = None
    registry: Any = None
    model: SemanticModel | None = None
    draft_model: str = ""
    project_root: Path = Path(".")
    home_dir: Path = Path.home()
    auth_store: Any = None
    tables: list[dict] = field(default_factory=list)
    samples: dict | None = None
    stats: dict | None = None


def _dataset_by_name(model: SemanticModel, name: str) -> SemanticDataset | None:
    key = str(name or "").strip().lower()
    return next((d for d in model.datasets if d.name.lower() == key), None)


def _find_field(
    model: SemanticModel, ref: str,
) -> tuple[SemanticDataset, SemanticField] | None:
    """字段引用 → (dataset, field);未声明/裸名歧义 → ``None``(不猜)。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if "." in ref:
        ds_name, fname = ref.split(".", 1)
        dataset = _dataset_by_name(model, ds_name)
        if dataset is None:
            return None
        found = next(
            (f for f in dataset.fields if f.name.lower() == fname.strip().lower()),
            None,
        )
        return (dataset, found) if found is not None else None
    hits = [
        (d, f)
        for d in model.datasets
        for f in d.fields
        if f.name.lower() == ref.lower()
    ]
    return hits[0] if len(hits) == 1 else None


def _metric_by_name(model: SemanticModel, name: str) -> SemanticMetric | None:
    return next((m for m in model.metrics if m.name == name), None)


def _metric_time_ref(model: SemanticModel, metric: SemanticMetric) -> str:
    """度量的聚合时间字段 → qualified ``dataset.field``(没有 → ``""``)。

    ``agg_time_dimension`` 可能已经是 ``dataset.field`` 或裸列名。裸名先在
    metric 自己锚定的数据集里找(消歧锚),再全模型唯一解析;歧义不猜。
    未声明时取 metric 数据集中第一个 ``is_time`` 字段。
    """
    raw = str(metric.agg_time_dimension or "").strip()
    if raw:
        if "." in raw:
            hit = _find_field(model, raw)
            return f"{hit[0].name}.{hit[1].name}" if hit else ""
        for ds_name in metric.datasets:
            dataset = _dataset_by_name(model, ds_name)
            if dataset is None:
                continue
            found = next(
                (f for f in dataset.fields if f.name.lower() == raw.lower()), None,
            )
            if found is not None:
                return f"{dataset.name}.{found.name}"
        hit = _find_field(model, raw)
        return f"{hit[0].name}.{hit[1].name}" if hit else ""
    for ds_name in metric.datasets:
        dataset = _dataset_by_name(model, ds_name)
        if dataset is None:
            continue
        found = next((f for f in dataset.fields if f.is_time), None)
        if found is not None:
            return f"{dataset.name}.{found.name}"
    return ""


def _dimension_pool(
    model: SemanticModel,
) -> list[tuple[SemanticDataset, SemanticField]]:
    """可分组/可过滤的字段池:非时间、角色为 dimension 或 enum。"""
    return [
        (dataset, f)
        for dataset in model.datasets
        for f in dataset.fields
        if not f.is_time and f.semantic_role in {"dimension", "enum"}
    ]


def enumerate_candidates(
    model: SemanticModel, *, limit: int, salt: str = DEFAULT_SALT,
) -> list[Candidate]:
    """从语义模型枚举五类候选,按 ``sha256(salt|key)`` 稳定排序后截断。

    排序与输入书写顺序无关(同一份模型在任何进程都得到同一前缀),这样
    ``limit`` 截断才是可复现的;``limit`` 同时被 ``MAX_GENERATE_PER_RUN``
    封顶(组合池再大也不会一次生成上百条)。
    """
    pool = _dimension_pool(model)
    candidates: list[Candidate] = []
    for metric in model.metrics:
        for dataset, f in pool:
            candidates.append(Candidate(
                kind="metric_dim",
                metric=metric.name,
                dimension=f"{dataset.name}.{f.name}",
            ))
        time_ref = _metric_time_ref(model, metric)
        if time_ref:
            for grain in CANDIDATE_GRAINS:
                candidates.append(Candidate(
                    kind="metric_grain", metric=metric.name, grain=grain,
                ))
                for dataset, f in pool:
                    candidates.append(Candidate(
                        kind="metric_dim_grain",
                        metric=metric.name,
                        dimension=f"{dataset.name}.{f.name}",
                        grain=grain,
                    ))
        for dataset, f in pool:
            ref = f"{dataset.name}.{f.name}"
            for value in f.values[:MAX_VALUES_PER_FIELD]:
                candidates.append(Candidate(
                    kind="metric_filter",
                    metric=metric.name,
                    field=ref,
                    value=str(value),
                ))
            candidates.append(Candidate(
                kind="metric_top_n",
                metric=metric.name,
                dimension=ref,
                top_n=TOP_N,
            ))

    # 同一 key = 同一候选;dict 保序,首个出现者留下。
    deduped = list({c.key(): c for c in candidates}.values())
    deduped.sort(
        key=lambda c: hashlib.sha256(
            f"{salt}|{c.key()}".encode("utf-8"),
        ).hexdigest(),
    )
    limit = max(1, min(int(limit), MAX_GENERATE_PER_RUN))
    return deduped[:limit]


def build_question(
    cand: Candidate, model: SemanticModel, *, lang: str,
) -> str:
    """候选 → 含自然名的问题文本(模板见 :mod:`evalset.templates`)。"""
    labels: dict[str, str] = {}
    metric = _metric_by_name(model, cand.metric)
    if metric is not None:
        labels[metric.name] = natural_name(
            "", metric.definition, metric.synonyms, metric.name,
        )
    for ref in (cand.dimension, cand.field):
        if not ref or ref in labels:
            continue
        hit = _find_field(model, ref)
        if hit is None:
            continue
        _dataset, f = hit
        labels[ref] = natural_name(
            f.label, f.description, f.synonyms, f.name,
        )
    return format_question(cand, labels, lang)


def _query_for(
    cand: Candidate, model: SemanticModel,
) -> SemanticQuery | None:
    """候选 → 声明式查询(时间字段解析不出来 → ``None``,算法上不会发生)。"""
    dimensions: list[str] = []
    if cand.kind in {"metric_dim", "metric_dim_grain", "metric_top_n"}:
        dimensions = [cand.dimension]
    time_grain: dict[str, str] | None = None
    if cand.kind in {"metric_grain", "metric_dim_grain"}:
        metric = _metric_by_name(model, cand.metric)
        time_ref = _metric_time_ref(model, metric) if metric is not None else ""
        if not time_ref:
            return None
        time_grain = {"field": time_ref, "grain": cand.grain}
    filters: list[dict[str, Any]] = []
    if cand.kind == "metric_filter":
        filters = [{"field": cand.field, "op": "=", "value": cand.value}]
    order_by: list[dict[str, Any]] = []
    limit: int | None = None
    if cand.kind == "metric_top_n":
        order_by = [{"column": cand.metric, "direction": "desc"}]
        limit = cand.top_n or TOP_N
    return SemanticQuery(
        metrics=[cand.metric],
        dimensions=dimensions,
        time_grain=time_grain,
        filters=filters,
        order_by=order_by,
        limit=limit,
    )


def _tags(cand: Candidate) -> dict[str, Any]:
    """候选 → 条目标签(闭集键:metric/dimension/time_grain/pattern)。"""
    tags: dict[str, Any] = {"metric": [cand.metric]}
    if cand.dimension:
        tags["dimension"] = [cand.dimension.rsplit(".", 1)[-1]]
    if cand.grain:
        tags["time_grain"] = [cand.grain]
    patterns: list[str] = []
    if cand.dimension:
        patterns.append("group_by")
    if cand.grain:
        patterns.append("time_series")
    if cand.kind == "metric_filter" and cand.field:
        patterns.append("filter")
    if cand.kind == "metric_top_n":
        patterns.append("top_n")
    if patterns:
        tags["pattern"] = patterns
    return tags


async def rewrite_questions(
    ctx: GenerateContext, questions: list[str],
) -> list[str] | None:
    """一次 LLM 调用改写全部问句;不可用/形状不符 → ``None``(调用方回落)。

    只改问法不改语义是**提示词约束**,不是运行期校验 —— 改写结果与输入
    一一对应(长度相同),下游仍拿模板问句的组件做标签。
    """
    if not questions:
        return []
    messages = [
        {"role": "system", "content": render("evalset/rewrite_system", lang=ctx.lang)},
        {"role": "user", "content": render(
            "evalset/rewrite_user",
            lang=ctx.lang,
            questions=json.dumps(questions, ensure_ascii=False),
        )},
    ]
    try:
        response = await ctx.llm.chat(
            model=ctx.draft_model,
            messages=messages,
            max_tokens=_REWRITE_MAX_TOKENS,
        )
    except Exception:  # noqa: BLE001 — 改写是装饰,失败必须回落到模板
        logger.warning("evalset rewrite: LLM call failed; keeping template questions")
        return None
    parsed = _parse_rewrites(response, len(questions))
    if parsed is None:
        logger.warning(
            "evalset rewrite: unusable LLM response; keeping template questions",
        )
    return parsed


def _parse_rewrites(response: Any, expected: int) -> list[str] | None:
    """``["..."]``(可带 ```json 围栏)→ 等长字符串列表;否则 ``None``。"""
    if not isinstance(response, str):
        return None
    text = response.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list) or len(data) != expected:
        return None
    if not all(isinstance(item, str) and item.strip() for item in data):
        return None
    return [item.strip() for item in data]


async def generate_semantic(
    ctx: GenerateContext, *, limit: int, rewrite: bool = False,
) -> SourceResult:
    """枚举 → 编译 → 试执行 → (可选)改写,返回候选条目。"""
    if ctx.model is None:
        return SourceResult([], status="no_semantic_model")

    model = ctx.model
    items: list[GeneratedItem] = []
    compile_misses = 0
    trial_failures = 0

    for cand in enumerate_candidates(model, limit=limit):
        query = _query_for(cand, model)
        if query is None:
            compile_misses += 1
            continue
        try:
            compiled = build_and_compile(model, query, dialect=ctx.dialect)
        except SemanticQueryError:
            # ``build_and_compile`` 把 CompileMiss 与 PartialCompile 都映射成
            # SemanticQueryError(声明式 API 的严格契约:未声明即拒),所以
            # 一个 except 覆盖「编译 MISS / 未完全解析」两种跳过。
            compile_misses += 1
            continue
        sql = str(compiled.get("sql") or "")
        if not sql or not sqlglot_ok(sql):
            # 编译器产物不可解析属于编译侧问题,不是执行侧失败。
            compile_misses += 1
            continue
        # registry 缺失但给了数据源 = 调用方保证的测试/无连接场景,跳过
        # 试执行;数据源缺失 = 无法验证,按 trial 失败跳过(不放没验证的 SQL)。
        if not (ctx.registry is None and ctx.datasource is not None):
            if not await trial_ok(ctx.registry, ctx.datasource, sql):
                trial_failures += 1
                continue
        items.append(GeneratedItem(
            question=build_question(cand, model, lang=ctx.lang),
            reference_sql=sql,
            source=SOURCE,
            reference_origin=ORIGIN,
            generator=GENERATOR,
            tags=_tags(cand),
            created_by="system",
        ))

    degraded: list[dict[str, Any]] = []
    if compile_misses:
        degraded.append({"code": "compile_miss", "count": compile_misses})
    if trial_failures:
        degraded.append({"code": "trial_failed", "count": trial_failures})

    if rewrite and ctx.llm is not None and ctx.draft_model and items:
        rewritten = await rewrite_questions(ctx, [it.question for it in items])
        if rewritten is None:
            degraded.append({
                "code": "rewrite_failed",
                "message": "LLM 改写不可用，保留模板问句",
            })
        else:
            for item, question in zip(items, rewritten):
                item.question = question

    return SourceResult(
        items=items,
        skipped=compile_misses + trial_failures,
        degraded=degraded,
        status="ok",
    )


__all__ = [
    "CANDIDATE_GRAINS",
    "GENERATOR",
    "GRAINS",
    "ORIGIN",
    "SOURCE",
    "Candidate",
    "GenerateContext",
    "GeneratedItem",
    "SourceResult",
    "build_question",
    "enumerate_candidates",
    "generate_semantic",
    "rewrite_questions",
]
