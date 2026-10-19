"""逐题回放 —— 生产查询管线的**前段**,headless。

回放的是生产里已经存在的那几个节点,不复制任何规划/编译逻辑:

``classify_intent → parse_date → schema_linking → fast_match → query_sketch``

之后**到此为止**:不进入 gen_sql、不执行 SQL、不读业务行、不判答案对错。
体检衡量的是「语义模型能否编译这批问题」,不是「回答是否正确」。

三条必须守住的边界:

- **零执行**:本模块从不调用 ``adapter.execute`` —— 单测注入一个 ``execute``
  必抛的适配器跑全链,断言它一次都没被调用。适配器只用来取 schema 与方言。
- **零额外 LLM**:唯一的模型调用是 ``query_sketch`` 自己的 NL→计划(快速档,
  ``resolve_sketch_model``);``CountingLLM`` 计数、记录实际模型、记最后一次
  调用错误,并在硬上限处**响亮**抛 :class:`CoverageBudgetExceeded`(触顶是
  运行级事件,绝不悄悄多花)。
- **一次 schema 读**:``prepare()`` 取一次 schema,构造语义 provider 与
  ``schema_map``;每题只复用它们(``schema_map`` 注入让 query_sketch 不再
  逐题读 schema)。

计划自相矛盾时生产会做**有界重规划**(``MAX_PLAN_REPLANS``);体检照抄同一
上限:至多 3 次节点调用。耗尽仍 pending → ``error:replan_not_converged``
(单列,不算覆盖也不能算缺口)。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from trove.core.config import AgentConfig
from trove.services.semantic_layer.factory import build_provider
from trove.services.question_coverage import taxonomy
from trove.services.question_coverage.gaps import build_gaps, synthetic_dataset_gap
from trove.workflow.intent import Intent, classify_intent
from trove.workflow.nodes.fast_match import make_fast_match
from trove.workflow.nodes.parse_date import make_parse_date
from trove.workflow.nodes.query_sketch import (
    MAX_PLAN_REPLANS,
    make_query_sketch,
    resolve_sketch_model,
)
from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.state import WorkflowState

#: 节点调用次数上限 = 初始 + 重规划(MAX_PLAN_REPLANS = 2)。
MAX_PLAN_INVOCATIONS = MAX_PLAN_REPLANS + 1

#: 与生产 ``graphs.py`` 的 MAX_REFLECT_RETRIES 同一份修正预算
#: (重规划要过 budget_exhausted 闸;体检照抄,不另发明一个数)。
SKETCH_MAX_RETRIES = 10

#: 预览有界(与 store 的列宽/产品承诺一致)。
PREVIEW_CHARS = 2000

#: 进入计划回放的意图(其余确定性判为 not_query,零 LLM)。
QUERY_INTENTS = frozenset({"query", "attribution"})


class CoverageBudgetExceeded(RuntimeError):
    """单题之外的硬调用上限被撞到 —— 运行级失败,不是某题的结果。"""

    def __init__(self, cap: int) -> None:
        super().__init__(f"coverage LLM call cap reached ({cap})")
        self.cap = cap


class CountingLLM:
    """``chat`` 的计数代理:计数 + 记录模型 + 记录最后一次失败。

    ``query_sketch`` 在 JSON 模式失败后会**无格式重试一次** —— 只有最后一次
    仍失败才算这题「LLM 出错」,所以每次调用**前**清空 ``last_call_error``。
    其余方法原样委托(网关的 mock/属性不做二道包装)。
    """

    def __init__(self, inner: Any, cap: int) -> None:
        self.inner = inner
        self.cap = int(cap)
        self.calls = 0
        self.models: list[str] = []
        self.last_call_error = ""
        self.exhausted = False

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        if model is None and args:
            model = args[0]
        if self.calls >= self.cap:
            self.exhausted = True
            self.last_call_error = "budget_exceeded"
            raise CoverageBudgetExceeded(self.cap)
        self.calls += 1
        self.last_call_error = ""
        if model:
            self.models.append(str(model))
        try:
            return await self.inner.chat(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 记录后原样上抛
            self.last_call_error = f"{type(exc).__name__}: {exc}"[:300]
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class Deterministic:
    """确定性前段(零 LLM)的结果。preflight 与 check 共用。"""

    state: WorkflowState
    intent: str
    no_model: bool = False
    unmatched: bool = False
    certified: bool = False
    sql: str = ""
    template_name: str = ""

    @property
    def needs_plan(self) -> bool:
        return (
            self.intent in QUERY_INTENTS
            and not self.certified
            and not self.unmatched
            and not self.no_model
        )


@dataclass
class Prepared:
    """一次 run 的公共准备(一次 schema 读,全部题共用)。"""

    adapter: Any
    schema: Any
    provider: Any
    schema_map: dict[str, set[str]] = field(default_factory=dict)

    @property
    def has_model(self) -> bool:
        return self.provider is not None


def schema_map_from(schema: Any) -> dict[str, set[str]]:
    """活 schema → ``query_sketch`` 的 ``schema_map``(表名/列名小写)。

    空库是合法值(空 dict ≠ 未提供);调用方凭它跳过逐题 schema 读。
    """
    return {
        t.name.lower(): {c.name.lower() for c in t.columns}
        for t in schema.tables
    }


def clip(text: Any, limit: int = PREVIEW_CHARS) -> str:
    """把任意值变成有界预览字符串(不改变内容,只截断)。"""
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def merge_update(
    state: WorkflowState, update: dict[str, Any] | None,
) -> tuple[WorkflowState, list[str]]:
    """节点更新 → 新 state;只接受 ``WorkflowState`` 已声明的字段。

    LangGraph 对普通值通道就是字段替换;这里模拟同一语义。未知键**丢弃并
    报告**(写进 detail 的 ``dropped_fields``)—— 静默接受未知键会让体检
    相信一个生产图根本收不到的字段。
    """
    if not update:
        return state, []
    known = WorkflowState.model_fields
    data: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in update.items():
        if key in known:
            data[key] = value
        else:
            dropped.append(str(key))
    return (state.model_copy(update=data) if data else state), dropped


class CoverageEngine:
    """一个 run 一个实例;``check`` 逐题调用(服务层顺序执行)。"""

    def __init__(
        self,
        *,
        kb: Any,
        connectors: Any,
        llm: Any,
        config: AgentConfig,
        datasource: str,
        run_id: str = "",
        user_id: str = "",
        lang: str | None = None,
        skills: Any = None,
        today: Callable[[], date] = date.today,
        max_llm_calls: int = 6,
        root: str | None = None,
    ) -> None:
        self.kb = kb
        self.connectors = connectors
        self.llm = llm
        self.config = config
        self.datasource = datasource
        self.run_id = run_id
        self.user_id = user_id
        self.lang = lang or getattr(config, "language", "zh") or "zh"
        self.skills = skills
        self.today = today
        self.max_llm_calls = max(1, int(max_llm_calls))
        self.root = root
        self.model, self.model_source = resolve_sketch_model(config)
        self._prepared: Prepared | None = None
        self._parse_date: Callable | None = None
        self._link: Callable | None = None
        self._fast: Callable | None = None

    # ── 准备 ────────────────────────────────────────────────────────────

    def configure(self, prepared: Prepared) -> None:
        """注入已取好的 schema/provider(preflight 复用同一份)。"""
        self._prepared = prepared
        self._nodes(prepared)

    async def prepare(self) -> Prepared:
        """取一次 schema、建 provider 与前段节点(幂等)。"""
        if self._prepared is None:
            adapter = await self.connectors.get(self.datasource or None)
            schema = await adapter.get_schema()
            provider = await build_provider(
                self.kb, self.config, self.datasource, adapter,
                schema=schema, root=self.root,
            )
            schema_map = schema_map_from(schema)
            self._prepared = Prepared(
                adapter=adapter, schema=schema, provider=provider,
                schema_map=schema_map,
            )
            self._nodes(self._prepared)
        return self._prepared

    def _nodes(self, prepared: Prepared) -> None:
        self._parse_date = make_parse_date(self.config, today=self.today)
        self._link = make_schema_linking(
            kb=self.kb, connectors=self.connectors,
            semantic_layer=prepared.provider,
        )
        self._fast = make_fast_match(
            self.kb, self.connectors, self.config, prepared.provider,
        )

    # ── 确定性前段 ──────────────────────────────────────────────────────

    async def deterministic(self, question: str) -> Deterministic:
        """意图 → parse_date → schema_linking → fast_match(零 LLM)。"""
        prepared = await self.prepare()
        intent_enum = classify_intent(question)
        intent = intent_enum.value if isinstance(intent_enum, Intent) else "query"

        state = WorkflowState(
            session_id=f"coverage:{self.run_id}" if self.run_id else "coverage",
            run_id=self.run_id,
            question=question,
            datasource=self.datasource,
            lang=self.lang,
            intent=intent,
            user_id=self.user_id,
            is_admin=True,
            tool_roles=["admin"],
            dialect="sqlite",
        )
        if intent not in QUERY_INTENTS:
            return Deterministic(state=state, intent=intent)

        # 方言:生产图上的节点自己也会解析;这里先取好,让 fast_match /
        # query_sketch 的 state.dialect 与 adapter 一致。
        try:
            state = state.model_copy(
                update={"dialect": prepared.adapter.dialect()})
        except Exception:
            pass

        assert self._parse_date and self._link and self._fast
        update = await self._parse_date(state)
        state, _ = merge_update(state, update)

        update = await self._link(state)
        state, _ = merge_update(state, update)
        no_model = bool((update or {}).get("no_model"))
        refusal_reason = str((state.refusal or {}).get("reason") or "")
        if no_model or refusal_reason == "no_semantic_match":
            return Deterministic(
                state=state, intent=intent, no_model=no_model, unmatched=True,
            )

        update = await self._fast(state)
        state, _ = merge_update(state, update)
        certified = bool((update or {}).get("fast_path") and (update or {}).get("sql"))
        if certified:
            hits = state.kb_hits or []
            template_name = ""
            if hits and isinstance(hits[0], dict):
                template_name = str(hits[0].get("question") or "")
            return Deterministic(
                state=state, intent=intent, certified=True,
                sql=str((update or {}).get("sql") or ""),
                template_name=template_name,
            )
        return Deterministic(state=state, intent=intent)

    # ── 逐题判定 ────────────────────────────────────────────────────────

    async def check(self, question: str, *, seq: int) -> dict[str, Any]:
        """回放一题 → coverage_result 行(store 列形状 + ``gaps``/``detail``)。

        单题异常(节点 bug / 非预算的 LLM 故障)折成一行 ``error``,不中断
        整轮;预算触顶与控制流取消原样上抛(运行级事件)。
        """
        started = time.monotonic()
        counter = CountingLLM(self.llm, self.max_llm_calls)
        prepared = await self.prepare()
        det: Deterministic | None = None
        try:
            det = await self.deterministic(question)
            if det.intent not in QUERY_INTENTS:
                return self._build(
                    seq, question, started, det=det, counter=counter,
                    outcome="not_query", outcome_reason=det.intent,
                )
            if det.certified:
                return self._build(
                    seq, question, started, det=det, counter=counter,
                    outcome="certified", outcome_reason="",
                )
            if det.unmatched or det.no_model:
                reason = "no_model" if det.no_model else "no_semantic_match"
                return self._build(
                    seq, question, started, det=det, counter=counter,
                    outcome="unmatched", outcome_reason=reason,
                )
            state = det.state
            assert prepared.schema_map is not None
            sketch = make_query_sketch(
                counter, self.config, connectors=self.connectors,
                semantic_layer=prepared.provider, skills=self.skills,
                schema_map=prepared.schema_map,
                max_retries=SKETCH_MAX_RETRIES,
            )
            dropped: list[str] = []
            invocations = 0
            for _ in range(MAX_PLAN_INVOCATIONS):
                update = await sketch(state)
                state, missing = merge_update(state, update)
                dropped.extend(missing)
                invocations += 1
                if counter.exhausted:
                    raise CoverageBudgetExceeded(counter.cap)
                if not state.plan_replan_pending:
                    break
            if state.plan_replan_pending:
                return self._build(
                    seq, question, started, det=det, state=state,
                    counter=counter, outcome="error",
                    outcome_reason="replan_not_converged",
                    extra={"invocations": invocations,
                           "dropped_fields": dropped},
                )
            meta = state.compile_meta or {}
            outcome, reason = taxonomy.classify(
                intent=det.intent,
                fast_path=False,
                no_model=False,
                refusal_reason=str((state.refusal or {}).get("reason") or ""),
                compile_meta=meta,
                plan_validation_status=str(
                    (state.plan_validation or {}).get("status") or ""),
                last_call_error=bool(counter.last_call_error),
                llm_calls=counter.calls,
            )
            return self._build(
                seq, question, started, det=det, state=state, counter=counter,
                outcome=outcome, outcome_reason=reason,
                extra={"invocations": invocations,
                       "dropped_fields": dropped},
            )
        except CoverageBudgetExceeded:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单题失败折成一行 error
            return self._build(
                seq, question, started, det=det, counter=counter,
                outcome="error",
                outcome_reason=f"node_error:{type(exc).__name__}",
                extra={"error": clip(f"{type(exc).__name__}: {exc}", 500)},
            )

    # ── 结果组装 ────────────────────────────────────────────────────────

    def _build(
        self,
        seq: int,
        question: str,
        started: float,
        *,
        det: Deterministic | None,
        counter: CountingLLM,
        outcome: str,
        outcome_reason: str,
        state: WorkflowState | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = state or (det.state if det is not None else None)
        meta = dict(state.compile_meta or {}) if state is not None else {}
        refusal = dict(state.refusal or {}) if state is not None else {}
        validated = dict(state.plan_validation or {}) if state is not None else {}
        sql = ""
        if det is not None and det.certified:
            sql = det.sql
        elif state is not None:
            sql = str(state.compiled_sql or "")
        miss_reason = str(meta.get("miss_reason") or "")
        miss_component = str(meta.get("miss_component") or "")
        if outcome in ("soft_miss", "refused") and not miss_reason:
            refusal_miss = refusal.get("compile_miss") or {}
            if isinstance(refusal_miss, dict):
                miss_reason = str(refusal_miss.get("reason") or "")
                miss_component = str(refusal_miss.get("component") or "")
        if outcome == "unmatched" and not miss_reason:
            miss_reason = outcome_reason

        gaps = self._gaps_for(outcome, miss_reason, state, meta)
        detail: dict[str, Any] = {
            "matched_tables": list(state.matched_tables) if state else [],
            "plan_preview": clip(state.plan if state else ""),
            "sql_preview": clip(sql),
            "plan_typed": bool(meta.get("plan_typed")),
            "compile_meta": meta,
            "plan_validation": {
                "status": str(validated.get("status") or ""),
                "errors": [clip(e, 300) for e in (validated.get("errors") or [])][:10],
            },
            "refusal_reason": str(refusal.get("reason") or ""),
            "no_model": bool(det.no_model) if det is not None else False,
            "llm_models": sorted(set(counter.models)),
            "configured_model": self.model,
            "model_source": self.model_source,
            "last_call_error": counter.last_call_error,
        }
        detail.update(extra or {})
        return {
            "seq": seq,
            "question": question,
            "outcome": outcome,
            "outcome_reason": outcome_reason,
            "intent": det.intent if det is not None else "query",
            "plan_typed": bool(meta.get("plan_typed")),
            "miss_class": str(meta.get("miss_class") or ""),
            "miss_reason": miss_reason,
            "miss_component": clip(miss_component),
            "template_name": clip(det.template_name if det is not None else "", 300),
            "llm_calls": counter.calls,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "gaps": gaps,
            "detail": detail,
        }

    def _gaps_for(
        self,
        outcome: str,
        miss_reason: str,
        state: WorkflowState | None,
        meta: dict[str, Any],
    ) -> list[dict[str, Any]]:
        model = None
        if self._prepared is not None and self._prepared.provider is not None:
            try:
                model = self._prepared.provider.model()
            except Exception:
                model = None
        if outcome == "partial" and state is not None:
            return build_gaps(
                list(state.compile_misses or []),
                model=model, datasource=self.datasource,
            )
        if outcome in ("soft_miss", "refused") and miss_reason:
            return build_gaps(
                [{"reason": miss_reason, "component": meta.get("miss_component") or ""}],
                model=model, datasource=self.datasource,
            )
        if outcome == "unmatched":
            return [synthetic_dataset_gap(
                miss_reason or "no_semantic_match", model=model,
                datasource=self.datasource)]
        return []


__all__ = [
    "CoverageBudgetExceeded",
    "CoverageEngine",
    "CountingLLM",
    "Deterministic",
    "MAX_PLAN_INVOCATIONS",
    "PREVIEW_CHARS",
    "SKETCH_MAX_RETRIES",
    "Prepared",
    "clip",
    "merge_update",
    "schema_map_from",
]
