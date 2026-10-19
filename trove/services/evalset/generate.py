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
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.prompts import render
from trove.services.evalset.leakage import check_leakage, load_example_questions
from trove.services.evalset.lock import EvalsetBusy, EvalsetLock
from trove.services.evalset.models import (
    DEFAULT_GENERATE_LIMIT,
    MAX_GENERATE_PER_RUN,
    EvalsetDoc,
    EvalsetItem,
    content_hash,
    new_qid,
    now_iso,
)
from trove.services.evalset.rev import evalset_rev
from trove.services.evalset.store import load_evalset, save_evalset
from trove.services.evalset.templates import (
    CANDIDATE_GRAINS,
    GRAINS,
    Candidate,
    format_question,
    natural_name,
)
from trove.services.kb.ledger import normalize_question
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


# ── 编排(去重 / 泄漏 / 落盘 / 幂等)────────────────────────────────
#
# 下面的编排层把各源产出的候选合并进 ``evalset.yml``。三条纪律:
#
# 1. **零自动确认**:落盘的每一条都是 ``pending``,没有任何路径写
#    ``confirmed``;
# 2. **锁非阻塞**:生成/确认/编辑共用 ``EvalsetLock``,拿不到立即返回
#    一份报告(degraded 记 ``evalset.busy``),绝不排队等待、绝不崩;
# 3. **坏资产不覆盖**:已有 ``evalset.yml`` 有任何解析 issue(含路径身份
#    不符)时返回 ``invalid_asset`` 并原样带回 issues,一个字节都不写。

#: 生成源闭集(与 ``models.SOURCES`` 不同:``manual``/``benchmark`` 是
#: 条目来源,不是生成源)。
GENERATE_SOURCES = ("semantic", "synthetic", "production")


@dataclass
class GenerateRequest:
    """一次生成请求(管理端/CLI 的输入形状)。"""

    datasource: str
    sources: list[str] = field(default_factory=lambda: ["semantic"])
    limit: int = DEFAULT_GENERATE_LIMIT
    lang: str = "zh"
    since: str = ""
    rewrite: bool = False


@dataclass
class GenerateReport:
    """一次生成的结果(``status`` 是结果状态,不是 HTTP 状态)。

    ``status`` 闭集:``invalid_asset | no_semantic_model | no_llm | empty |
    ok``(优先级同序)。``by_source`` 只统计**新增**条目;``duplicates`` /
    ``conflicts`` / ``leakage`` 都是「本次新条目」上的计数 —— 幂等重跑时
    added=0、duplicates=上一轮的条数。
    """

    datasource: str
    status: str = "empty"
    added: int = 0
    skipped: int = 0
    duplicates: int = 0
    conflicts: int = 0
    leakage: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    degraded: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    evalset_rev: str = ""


def _effective_limit(limit: int) -> int:
    """``limit<=0`` → 默认条数;其余封顶到 ``MAX_GENERATE_PER_RUN``。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        value = DEFAULT_GENERATE_LIMIT
    return min(value, MAX_GENERATE_PER_RUN)


def _examples_path(kb: Any, datasource: str, root: Path) -> Path:
    """泄漏检查读的 ``examples.yml``(KB 目录优先,缺省回退根目录约定)。"""
    kb_dir = getattr(kb, "kb_dir", None)
    base = Path(kb_dir) if kb_dir is not None else (
        Path(root) / ".trove" / "kb")
    return base / datasource / "examples.yml"


async def _dialect(registry: Any, datasource: str) -> str:
    """从活动连接器探测方言;不可得一律回退 ``sqlite``(编译默认档)。

    真实 ``ConnectorRegistry.get`` 是 async,测试/服务层的 fake 是同步
    的 —— 两种都接受(``inspect.isawaitable``),不因为调用形状不同把一次
    生成打崩。
    """
    getter = getattr(registry, "get", None)
    if not callable(getter):
        return "sqlite"
    try:
        adapter = getter(datasource)
        if inspect.isawaitable(adapter):
            adapter = await adapter
        dialect = getattr(adapter, "dialect", None)
        value = dialect() if callable(dialect) else None
        if value:
            return str(value)
    except Exception:  # noqa: BLE001 — 方言探测失败不阻断生成
        logger.warning(
            "evalset generate: dialect probe failed for %s; assuming sqlite",
            datasource,
        )
    return "sqlite"


async def _load_tables(registry: Any, datasource: str) -> list[dict[str, Any]]:
    """synthetic 提示词用的表清单(读不到 → 空列表,synthetic 自行跳过)。"""
    getter = getattr(registry, "get_schema", None)
    if not callable(getter):
        return []
    try:
        schema = getter(datasource)
        if inspect.isawaitable(schema):
            schema = await schema
        return [
            {
                "name": table.name,
                "columns": [
                    {"name": col.name, "type": col.type}
                    for col in getattr(table, "columns", [])
                ],
            }
            for table in getattr(schema, "tables", [])
        ]
    except Exception:  # noqa: BLE001 — schema 读不到 = synthetic 无素材
        logger.warning(
            "evalset generate: schema load failed for %s; "
            "synthetic generation will be skipped", datasource,
        )
        return []


def _resolve_model(
    kb: Any,
    semantic_layer: Any,
    datasource: str,
    dialect: str,
    root: Path,
) -> tuple[Any, Any]:
    """语义模型解析:显式 provider 优先,其次按 KB 的 ``semantics.yml`` 建。

    返回 ``(provider, model)``。provider 缺失/``model()`` 抛错/模型为空都
    归一成 ``(…, None)`` —— 调用方据此返回 ``no_semantic_model``,而不是
    伪造一批注定编译不过的题。
    """
    provider = semantic_layer
    if provider is None:
        semantics_path = getattr(kb, "semantics_path", None)
        if not callable(semantics_path):
            return None, None
        try:
            path = Path(semantics_path(datasource))
            # directory 是补充源目录(与 KB 真源分开):把 kb 目录本身当
            # directory 会把 examples.yml 等资产也当语义文件解析。
            from trove.services.semantic_layer.provider import (
                SemanticLayerProvider,
            )
            provider = SemanticLayerProvider(
                directory=Path(root) / ".trove" / "semantic" / datasource,
                datasource=datasource,
                dialect=dialect,
                kb_semantics_path=path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "evalset generate: semantic provider unavailable for %s: %s",
                datasource, e,
            )
            return None, None
    try:
        model = provider.model()
    except Exception as e:  # noqa: BLE001 — provider 契约是从不上抛
        logger.warning(
            "evalset generate: semantic model load failed for %s: %s",
            datasource, e,
        )
        return provider, None
    return provider, model


def _merge_generated(
    doc: EvalsetDoc,
    items: Any,
    *,
    examples: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """把一批候选合并进 ``doc``(原地追加),返回本次计数。

    候选是**鸭子类型**的(Task 5/6 的 ``GeneratedItem`` 没有 qid/status/
    hash):身份一律由这里分配 —— ``new_qid()`` + 重算 ``content_hash``,
    候选自己报的哈希不被采信(否则两个源可以各自发明哈希空间)。

    去重规则(设计 §5.2):

    - 已有条目 ``content_hash`` 相同(``rejected`` 墓碑参与、``retired``
      不参与)→ ``duplicates += 1``,丢弃;
    - 同 ``ledger.normalize_question`` 但哈希不同 → 照常落盘,标
      ``duplicate_of=<已有 qid>``,``conflicts += 1``(同题不同 SQL 由
      管理员裁决,绝不静默覆盖);
    - 越过前两条、真正落盘的条目做泄漏检查(``examples.yml`` 的全部
      示例),命中只**标记** ``leakage``/``leakage_hit``,条目仍是
      ``pending``。

    同一批候选之间同样适用上述两条(后出现的重复被丢弃 / 标冲突)。
    """
    stats: dict[str, Any] = {
        "added": 0, "duplicates": 0, "conflicts": 0, "leakage": 0,
        "skipped": 0, "by_source": {},
    }
    seen_hashes: set[str] = set()
    question_refs: dict[str, str] = {}

    for existing in doc.items:
        if existing.status == "retired":
            # retired 不参与去重:数据/口径可能已经变了,允许重新生成。
            continue
        seen_hashes.add(
            existing.content_hash
            or content_hash(existing.question, existing.reference_sql)
        )
        key = normalize_question(existing.question)
        if key and existing.qid:
            question_refs.setdefault(key, existing.qid)

    for gen in items or ():
        question = str(getattr(gen, "question", "") or "").strip()
        if not question:
            stats["skipped"] += 1
            continue
        reference_sql = str(getattr(gen, "reference_sql", "") or "")
        digest = content_hash(question, reference_sql)
        if digest in seen_hashes:
            stats["duplicates"] += 1
            continue

        key = normalize_question(question)
        duplicate_of = question_refs.get(key, "")
        item = EvalsetItem(
            qid=new_qid(),
            question=question,
            reference_sql=reference_sql,
            source=str(getattr(gen, "source", "") or ""),
            reference_origin=str(getattr(gen, "reference_origin", "") or ""),
            status="pending",  # 生成永不写 confirmed
            tags=dict(getattr(gen, "tags", None) or {}),
            content_hash=digest,
            needs_reference=bool(getattr(gen, "needs_reference", False)),
            duplicate_of=duplicate_of,
            note=str(getattr(gen, "note", "") or ""),
            created_at=now_iso(),
            created_by=str(getattr(gen, "created_by", "") or "system"),
            generator=str(getattr(gen, "generator", "") or ""),
        )
        if duplicate_of:
            stats["conflicts"] += 1

        # Task 3 的 ``LeakageHit.question`` 是命中的**示例**问句;标记必须
        # 用条目自己的问题(命中说明另存 ``leakage_hit``)。
        hit = check_leakage(question, list(examples))
        if hit is not None:
            item.leakage = True
            item.leakage_hit = hit.note()
            stats["leakage"] += 1

        doc.items.append(item)
        seen_hashes.add(digest)
        if key:
            question_refs.setdefault(key, item.qid)
        stats["added"] += 1
        by_source = stats["by_source"]
        by_source[item.source] = by_source.get(item.source, 0) + 1

    return stats


def _commit_evalset(kb: Any, path: Path, report: GenerateReport) -> None:
    """有新条目才调用;只暂存 ``path`` 这一个文件,失败降级为 degraded。"""
    git = getattr(kb, "git", None)
    if git is None:
        return  # 非 git 环境是正常形态,不是降级
    commit_files = getattr(git, "commit_files", None)
    if not callable(commit_files):
        report.degraded.append({
            "code": "git_unavailable",
            "message": "kb.git has no commit_files(); evalset was saved unversioned",
        })
        return
    message = (
        f"evalset: generate {report.added} item(s) for {report.datasource}"
    )
    try:
        result = commit_files([path], message, trailers={"Generator": "evalset"})
    except Exception as e:  # noqa: BLE001 — 版本化绝不阻断写入
        logger.warning("evalset generate: git commit failed: %s", e)
        report.degraded.append({"code": "git_commit_failed", "message": str(e)})
        return
    if isinstance(result, dict) and result.get("committed") is False:
        reason = str(result.get("reason") or "")
        if reason != "nothing-to-commit":
            report.degraded.append({
                "code": "git_not_committed", "reason": reason,
            })


async def generate_evalset(
    root: Path,
    request: GenerateRequest,
    *,
    kb: Any,
    registry: Any = None,
    llm: Any = None,
    draft_model: str = "",
    semantic_layer: Any = None,
    auth_store: Any = None,
    home_dir: Path | None = None,
) -> GenerateReport:
    """跑请求里点名的生成源,合并去重后原子落盘(可选 git 提交)。

    ``status`` 优先级:``invalid_asset > no_semantic_model > no_llm >
    empty > ok``。锁是非阻塞的:另一个写者持锁时返回 added=0 的报告并在
    ``degraded`` 记 ``evalset.busy`` —— 不排队、不抛给调用方。
    """
    root = Path(root)
    datasource = request.datasource
    report = GenerateReport(datasource=datasource)
    lock = EvalsetLock(root / ".trove" / "evalsets")

    try:
        with lock.acquire(datasource):
            doc, issues, _exists = load_evalset(root, datasource)
            report.issues = [issue.as_dict() for issue in issues]
            if issues:
                # 坏资产绝不覆盖:issues 原样带回,不写盘。
                report.status = "invalid_asset"
                report.evalset_rev = evalset_rev(doc)
                return report

            limit = _effective_limit(request.limit)
            dialect = await _dialect(registry, datasource)
            _provider, model = _resolve_model(
                kb, semantic_layer, datasource, dialect, root,
            )
            requested = [str(name) for name in (request.sources or [])]

            if "semantic" in requested and model is None:
                # 提前返回:不落其他源(语义模型是主视角,缺了它这批题
                # 就只剩覆盖题;提示先 /kb init 才是诚实的下一步)。
                report.status = "no_semantic_model"
                report.evalset_rev = evalset_rev(doc)
                return report

            ctx = GenerateContext(
                datasource=datasource,
                lang=request.lang,
                dialect=dialect,
                llm=llm,
                registry=registry,
                model=model,
                draft_model=draft_model,
                project_root=root,
                home_dir=Path(home_dir) if home_dir is not None else Path.home(),
                auth_store=auth_store,
                tables=await _load_tables(registry, datasource)
                if "synthetic" in requested else [],
            )
            examples = load_example_questions(
                _examples_path(kb, datasource, root),
            )

            # Task 6 carry-forward:sources 在模块级 import generate,这里
            # 必须延迟 import,否则成环。
            from trove.services.evalset import sources as source_mod

            produced = False
            # 状态优先级 no_llm > empty:点了 synthetic 却没有 LLM,且这一轮
            # 什么都没产出 → 报 no_llm,不能读成「跑过了、题就是 0 条」。
            llm_unavailable = "synthetic" in requested and llm is None
            for name in requested:
                result: SourceResult | None = None
                try:
                    if name == "semantic":
                        result = await generate_semantic(
                            ctx, limit=limit, rewrite=request.rewrite,
                        )
                    elif name == "synthetic":
                        if llm is None:
                            report.degraded.append({
                                "code": "no_llm",
                                "source": name,
                                "message": "LLM 不可用",
                            })
                            continue
                        result = await source_mod.generate_synthetic(
                            ctx, limit=limit,
                        )
                    elif name == "production":
                        result = await source_mod.collect_production(
                            ctx, limit=limit, since=request.since,
                        )
                    else:
                        report.degraded.append({
                            "code": "unknown_source",
                            "source": name,
                            "message": (
                                f"unknown generation source {name!r}; "
                                f"expected one of {', '.join(GENERATE_SOURCES)}"
                            ),
                        })
                        continue
                except Exception as e:  # noqa: BLE001 — 一个源失败不吞别的源
                    logger.warning(
                        "evalset generate: source %s failed: %s", name, e,
                    )
                    report.degraded.append({
                        "code": "source_failed",
                        "source": name,
                        "message": str(e),
                    })
                    continue

                report.skipped += result.skipped
                report.degraded.extend(result.degraded)
                if result.status == "no_llm":
                    report.degraded.append({
                        "code": "no_llm",
                        "source": name,
                        "message": "LLM 不可用",
                    })
                if not result.items:
                    continue
                produced = True
                stats = _merge_generated(doc, result.items, examples=examples)
                report.added += stats["added"]
                report.skipped += stats["skipped"]
                report.duplicates += stats["duplicates"]
                report.conflicts += stats["conflicts"]
                report.leakage += stats["leakage"]
                for source_name, count in stats["by_source"].items():
                    report.by_source[source_name] = (
                        report.by_source.get(source_name, 0) + count
                    )

            if report.added:
                path = save_evalset(root, doc)
                _commit_evalset(kb, path, report)

            report.evalset_rev = evalset_rev(doc)
            if produced:
                report.status = "ok"
            elif llm_unavailable:
                report.status = "no_llm"
            else:
                report.status = "empty"
            return report
    except EvalsetBusy as exc:
        # 非阻塞锁:把「另一个写者正在写」作为一份报告交出去(added=0),
        # 而不是让调用方看到一个原样冒出的 flock 异常。revision 仍可只读
        # 计算(资产写入是 os.replace 原子换名,读到的是一份完整版本)。
        report.status = "empty"
        report.degraded.append({"code": "evalset.busy", "message": str(exc)})
        try:
            busy_doc, _issues, _exists = load_evalset(root, datasource)
            report.evalset_rev = evalset_rev(busy_doc)
        except Exception:  # noqa: BLE001 — busy 报告不因读失败再崩一次
            pass
        return report


__all__ = [
    "CANDIDATE_GRAINS",
    "GENERATE_SOURCES",
    "GENERATOR",
    "GRAINS",
    "ORIGIN",
    "SOURCE",
    "Candidate",
    "GenerateContext",
    "GenerateReport",
    "GenerateRequest",
    "GeneratedItem",
    "SourceResult",
    "build_question",
    "enumerate_candidates",
    "generate_evalset",
    "generate_semantic",
    "rewrite_questions",
]
