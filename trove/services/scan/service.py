"""ScanService —— 规格 → 确定性发现 → **待确认**的决策规则草稿。

三段职责,边界刻意不混:

  1. ``scan()`` —— 纯确定性:解析窗口、逐单元扫描、记账(查询预算 +
     判不了的单元),产出 ``ScanReport``。零 LLM。
  2. ``scan_and_draft()`` —— 把超带发现翻译成 ``DecisionRule``,**经
     决策草稿门**写进 ``decision_drafts.yml``(pending-only)。草稿不是
     规则:执行面的任何一条路径都读不到它,直到管理员确认。自动内容
     永远不绕过确认门。
  3. ``run(job)`` —— 定时任务的入口:包住上面两段 + 可选的一轮假设
     (LLM 起草 → 确定性裁决)。一切失败折进报告(响亮,不抛给调度器)。

执行面是**注入的 runner**(``async (sql, datasource) -> (columns, rows)``),
与 ``AnalysisEngine`` 的 HopRunner 同款 —— 本包物理上够不到连接器注册表
(见 ``tests/services/scan/test_scan_readonly_posture.py``)。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger
from trove.services.analysis.budget import QueryLedger
from trove.services.decision.drafts import DecisionDraftStore
from trove.services.decision.rules import (
    Baseline,
    DecisionRule,
    Subject,
)
from trove.services.scan import scanner
from trove.services.scan.models import Finding, ScanError, ScanSpec, slug

logger = get_logger(__name__)

#: 证据节保留的查询条数(报告进 run.result_json,长文本列要有界)。
MAX_EVIDENCE_ENTRIES = 20

HopRunner = Callable[[str, str], Awaitable[tuple[list[str], list[list[Any]]]]]
DialectOf = Callable[[str], Awaitable[str]]

_CJK_RE = re.compile(r"[一-鿿]")


def _lang_of(text: str) -> str:
    """窗口文本的语言决定解析规则(与 ``DecisionService._lang_of`` 同款)。"""
    return "zh" if _CJK_RE.search(text or "") else "en"


def _num_literal(value: float) -> str:
    """float → 条件语言能解析的字面量(**禁指数记法**)。

    条件语法里的 NUMBER 是 ``\\d+(\\.\\d+)?`` —— ``1e-05`` 会解析失败,
    而一条解析不了的草稿连 lint 都过不去。小到六位小数截没了就再退十位,
    截不完就记 0(带宽以秒/分为单位的量级不会真的落在 1e-9 以下)。

    nan/inf 绝不进条件文本(``nan`` 是合法 token 吗?不是 —— 它会让
    整条草稿倒在 lint 上,而阈值的来处(``stats.band``)已经保证有限值;
    这里只是形状兜底)。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(f):
        return "0"
    text = f"{f:.6f}".rstrip("0").rstrip(".")
    if text in ("", "-", "-0", "0") and f != 0.0:
        text = f"{f:.10f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class ScanReport:
    """一次扫描的报告(也是定时任务 run 的证据体)。"""

    datasource: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    window: str = ""
    period: list[str] = field(default_factory=list)
    grain: str = ""
    mode: str = "trailing"
    lookback: int = 0
    units: int = 0
    findings: list[Finding] = field(default_factory=list)
    unverifiable: list[Finding] = field(default_factory=list)
    dropped: int = 0
    drafts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    hypothesis_rejected: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "datasource": self.datasource,
            "spec": dict(self.spec),
            "window": self.window,
            "period": list(self.period),
            "grain": self.grain,
            "mode": self.mode,
            "lookback": int(self.lookback),
            "units": int(self.units),
            "findings": [f.to_dict() for f in self.findings],
            "unverifiable": [f.to_dict() for f in self.unverifiable],
            "dropped": int(self.dropped),
            "drafts": [dict(d) for d in self.drafts],
            "errors": list(self.errors),
            "budget": dict(self.budget),
            "evidence": [dict(e) for e in self.evidence[:MAX_EVIDENCE_ENTRIES]],
        }
        if self.hypotheses:
            out["hypotheses"] = [dict(h) for h in self.hypotheses]
        if self.hypothesis_rejected:
            out["hypothesis_rejected"] = [dict(h) for h in self.hypothesis_rejected]
        if self.error:
            out["error"] = self.error
        return out

    def message(self) -> str:
        """一行摘要(通知正文 / run.verdict 旁的可读行)。"""
        head = f"[scan] {len(self.findings)} 个超带发现"
        if self.unverifiable:
            head += f" / {len(self.unverifiable)} 个判不了"
        if self.dropped:
            head += f" / 截断 {self.dropped}"
        parts = [head]
        for f in self.findings[:5]:
            z = f"{f.z:.1f}" if isinstance(f.z, (int, float)) else "—"
            parts.append(f"{f.label}: {f.current} (z={z})")
        return " | ".join(parts)


class ScanService:
    """扫描的编排者。实例可复用(每次 ``run``/``scan`` 各自开一份账本)。"""

    def __init__(
        self,
        kb,
        semantic_dir: str | Path | None = None,
        *,
        runner: HopRunner | None = None,
        dialect_of: DialectOf | None = None,
        config: Any = None,
        llm: Any = None,
        timeout_ms: int = 30_000,
    ) -> None:
        self.kb = kb
        self._semantic_dir = Path(semantic_dir) if semantic_dir is not None else None
        #: 只读一跳的执行面(注入)。缺席 → 扫描报错,绝不静默产出空报告。
        self._runner = runner
        self._dialect_of = dialect_of
        self.config = config
        self._llm = llm
        try:
            ms = int(timeout_ms)
        except (TypeError, ValueError):
            ms = 0
        self._timeout_ms = ms if ms > 0 else 30_000

    # ── 语义层 / 方言 / 窗口 ─────────────────────────────

    def _semantic_root(self) -> Path:
        if self._semantic_dir is not None:
            return self._semantic_dir
        if self.kb is not None:
            return Path(self.kb.kb_dir).parent / "semantic"
        return Path.cwd() / ".trove" / "semantic"

    def _provider_for(self, datasource: str, dialect: str):
        """每数据源一个语义 provider(与决策判定同一条构造路径)。"""
        from trove.services.semantic_layer.provider import SemanticLayerProvider

        kb_path = None
        if self.kb is not None:
            try:
                kb_path = self.kb.semantics_path(datasource)
            except Exception:
                kb_path = None
        return SemanticLayerProvider(
            directory=self._semantic_root() / datasource,
            datasource=datasource,
            dialect=dialect,
            kb_semantics_path=kb_path,
        )

    async def _dialect(self, datasource: str) -> str:
        if self._dialect_of is None:
            raise ScanError(
                "scan service has no dialect resolver (dialect_of=...) — "
                "拒绝猜方言:错误方言编译出的 SQL 是没人看得见的错答案")
        try:
            dialect = str(await self._dialect_of(datasource) or "")
        except Exception as e:
            raise ScanError(f"datasource unreachable: {datasource!r}: {e}") from e
        if not dialect:
            raise ScanError(f"cannot determine SQL dialect for {datasource!r}")
        return dialect

    @staticmethod
    def resolve_window(window: str, anchor: date) -> tuple[str, str] | None:
        """自然语言窗口 → ISO (start, end);解析不了 → None。

        走**公共**时间入口(与决策规则、问答管线同一份解析器):同一个
        「本月」在三处必须是同一个跨度。
        """
        text = str(window or "").strip()
        if not text:
            return None
        from trove.workflow.nodes.parse_date import format_time_range, parse_time_range

        rng = parse_time_range(text, reference_date=anchor, lang=_lang_of(text))
        if rng is None:
            return None
        ctx = format_time_range(*rng)
        try:
            start, end = ctx.split(" ~ ", 1)
        except ValueError:  # pragma: no cover - format_time_range 的契约
            return None
        return (start.strip(), end.strip())

    # ── 扫描 ────────────────────────────────────────────

    def _limits(self) -> dict[str, Any]:
        cfg = getattr(self.config, "scan", None)
        return {
            "max_queries": max(1, int(getattr(cfg, "max_queries", 12))),
            "top_k": max(1, int(getattr(cfg, "top_k", 5))),
            "lookback": max(1, int(getattr(cfg, "lookback", 12))),
            "k": float(getattr(cfg, "k", 3.5)),
        }

    async def scan(
        self, spec: ScanSpec, datasource: str, now: datetime | None = None,
    ) -> ScanReport:
        """确定性扫描 → 报告。设置类问题(无 runner/无模型/窗口解析不了)抛 ``ScanError``。"""
        if self._runner is None:
            raise ScanError("scan service has no query runner (runner=...)")
        anchor = (now or datetime.now()).date()
        cur_window = self.resolve_window(spec.window, anchor)
        if cur_window is None:
            raise ScanError(
                f"window {spec.window!r} is not a resolvable time expression "
                f"(reference date {anchor.isoformat()})")
        dialect = await self._dialect(datasource)
        provider = self._provider_for(datasource, dialect)
        if provider.model() is None:
            raise ScanError(
                f"no semantic model for datasource {datasource!r} — "
                "扫描在语义层上编译(未建模的数据源先 /kb init)")

        limits = self._limits()
        ledger = QueryLedger(total=limits["max_queries"])
        evidence: list[dict[str, Any]] = []
        units = spec.units
        found: list[Finding] = []
        for metric_name, dim_name in units:
            found.extend(await scanner.scan_unit(
                semantic_layer=provider, runner=self._runner,
                datasource=datasource, dialect=dialect, spec=spec,
                metric_name=metric_name, dim_name=dim_name,
                cur_window=cur_window, ledger=ledger, evidence=evidence,
            ))
        kept, dropped = scanner.cap_findings(found, limits["top_k"])
        return ScanReport(
            datasource=datasource,
            spec=spec.to_dict(),
            window=spec.window,
            period=[cur_window[0], cur_window[1]],
            grain=str(spec.grain or ""),
            mode=spec.mode,
            lookback=int(spec.lookback),
            units=len(units),
            findings=[f for f in kept if f.kind == "anomaly"],
            unverifiable=[f for f in kept if f.kind != "anomaly"],
            dropped=dropped,
            budget=ledger.snapshot(),
            evidence=evidence,
        )

    # ── 草稿 ────────────────────────────────────────────

    def _draft_rule(self, finding: Finding, spec: ScanSpec) -> DecisionRule:
        """超带发现 → 规则草稿(**确定性**:同一条发现在任何一次扫描里
        得到同一个 id 与同一个阈值)。

        阈值取**噪声带边**(不是观测值):观测值阈值一写下去,下个周期
        只要还在同一条带上就会自触发 —— 一条自我实现的规则。带边是
        「这与它的历史不符」这句话的字面翻译;条件语言里没有带语义
        (那是 B2 的 seasonal/significance),所以这里落成 current 与
        带边的比较,并在 note 里写明来处 —— 草稿是给人改的起点,不是
        终点。

        ``enabled=False`` 是**保真携带**的安全默认(与 preset 决策模板
        同款):确认只把规则送进执行面,「启用」是管理员的另一个明确
        动作 —— 自动内容连"确认即生效"都不该替人决定。
        """
        lo = finding.band.get("lo")
        hi = finding.band.get("hi")
        if finding.current is None or (lo is None and hi is None):
            raise ScanError(
                f"finding {finding.label!r} has no comparable band edge")
        above = hi is not None and (lo is None or finding.current >= hi)
        edge = hi if above else lo
        op = ">=" if above else "<="
        name = f"{finding.metric} 超出噪声带" if not finding.dimension else \
            f"{finding.metric}（{finding.dimension}={finding.value}）超出噪声带"
        rid = "-".join(x for x in (
            "scan", slug(finding.metric),
            slug(finding.dimension) if finding.dimension else "",
            slug(finding.value) if finding.value else "",
        ) if x)
        filters: list[dict[str, Any]] = []
        if finding.dimension and finding.value:
            filters.append({
                "field": finding.dimension, "op": "=", "value": finding.value,
            })
        return DecisionRule(
            id=rid,
            name=name,
            enabled=False,
            severity="warning",
            window=spec.window,
            subject=Subject(metrics=[finding.metric], filters=filters),
            baseline=Baseline(kind="none"),
            scope="aggregate",
            conditions=[f"current {op} {_num_literal(float(edge))}"],
            driver_dimension=finding.dimension or "",
        )

    async def draft_finding(
        self, finding: Finding, spec: ScanSpec, datasource: str,
    ) -> dict[str, Any]:
        """一条发现 → 一份 pending 草稿(``{"status": created|exists|present}``)。"""
        if self.kb is None:
            raise ScanError("scan service has no KB to write drafts through")
        rule = self._draft_rule(finding, spec)
        store = DecisionDraftStore(self.kb)
        return await _add_draft(store, datasource, rule, finding)

    async def scan_and_draft(
        self, spec: ScanSpec, datasource: str, now: datetime | None = None,
    ) -> ScanReport:
        """扫描 + 每条超带发现落一份 pending 草稿(草稿失败记账,不吞)。"""
        report = await self.scan(spec, datasource, now)
        if self.kb is None:
            return report
        store = DecisionDraftStore(self.kb)
        for finding in report.findings:
            try:
                rule = self._draft_rule(finding, spec)
                result = await _add_draft(store, datasource, rule, finding)
                report.drafts.append({
                    "id": rule.id,
                    "status": result.get("status", ""),
                    "draft_id": (result.get("draft") or {}).get("id", ""),
                    "label": finding.label,
                })
            except Exception as e:  # noqa: BLE001 — 一条草稿失败不改判定,但要响
                logger.warning("scan draft failed for %s: %s", finding.label, e)
                report.errors.append(f"draft {finding.label}: {str(e)[:200]}")
        return report

    # ── 定时任务入口 ─────────────────────────────────────

    async def run(self, job: Any, now: datetime | None = None) -> ScanReport:
        """跑一条扫描任务;一切失败折进报告(status=error 由 runner 记)。"""
        datasource = str(getattr(job, "datasource", "") or "")
        raw = getattr(job, "scan_spec", "") or ""
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
            spec = ScanSpec.from_dict(data)
        except (TypeError, ValueError) as e:
            return ScanReport(
                datasource=datasource,
                error=f"bad scan_spec: {str(e)[:300]}")
        try:
            report = await self.scan_and_draft(spec, datasource, now)
        except Exception as e:  # noqa: BLE001 — 设置类失败(无 runner/无模型/坏窗口)
            logger.warning("scan job %s failed: %s", getattr(job, "id", "?"), e)
            return ScanReport(
                datasource=datasource, spec=spec.to_dict(),
                window=spec.window, error=str(e)[:300])
        if spec.hypotheses and report.findings:
            if self._llm is None:
                # 规格要求了假设、却没有 LLM 出口 —— 静默跳过等于「跑了但没
                # 产出」:管理员打开开关后什么都看不到。记账,不吞。
                report.errors.append("hypotheses: no LLM gateway configured")
            else:
                await self._hypothesis_round(report, spec, datasource, now)
        return report

    async def _hypothesis_round(
        self, report: ScanReport, spec: ScanSpec, datasource: str,
        now: datetime | None,
    ) -> None:
        """扫到发现后(可选)一轮假设:LLM 起草 → 确定性裁决。

        best-effort 且**记账在报告里**:假设层失败只是 hypotheses 缺席 +
        errors 一行 —— 扫描结论已经成立,附录不拖垮正文(与决策桥同纪律)。
        """
        from trove.services.scan import hypotheses as hyp

        cfg = getattr(self.config, "scan", None)
        limit = max(1, int(getattr(cfg, "max_hypotheses", 3)))
        try:
            anchor = (now or datetime.now()).date()
            cur_window = self.resolve_window(spec.window, anchor)
            dialect = await self._dialect(datasource)
            provider = self._provider_for(datasource, dialect)
            base_window = None
            if cur_window is not None:
                from trove.core.periods import base_period

                refined = base_period(
                    f"{cur_window[0]} ~ {cur_window[1]}", "prev_period")
                base_window = refined[1] if refined else None
            ledger = QueryLedger(total=max(2, int(getattr(cfg, "max_queries", 12))))
            evidence: list[dict[str, Any]] = []
            accepted, rejected = await hyp.propose(
                self._llm, semantic_layer=provider,
                data=hyp.findings_block(report.findings),
                lang=_lang_of(spec.window) if spec.window else "zh",
                limit=limit,
            )
            verified = await hyp.verify(
                accepted, semantic_layer=provider, runner=self._runner,
                datasource=datasource, dialect=dialect,
                window=cur_window, base_window=base_window,
                ledger=ledger, evidence=evidence,
            )
            report.hypotheses = [v.to_dict() for v in verified]
            report.hypothesis_rejected = rejected
            report.evidence.extend(evidence)
        except Exception as e:  # noqa: BLE001
            logger.warning("scan hypothesis round failed: %s", e)
            report.errors.append(f"hypotheses: {str(e)[:200]}")


def rule_note(finding: Finding) -> str:
    """草稿 note:发现的证据去向(人审核时第一眼要看的东西)。"""
    lo = finding.band.get("lo")
    hi = finding.band.get("hi")
    z = "—" if finding.z is None else f"{finding.z:.2f}"
    cur = "—" if finding.current is None else f"{finding.current:g}"
    return (
        f"主动扫描发现:{finding.window or '(无窗口)'} "
        f"当期 {cur}, 噪声带 [{lo}, {hi}], z={z}, n={finding.n}"
        + ("(样本不足)" if finding.low_n else "")
    )


async def _add_draft(
    store: DecisionDraftStore, datasource: str, rule: DecisionRule,
    finding: Finding,
) -> dict[str, Any]:
    """草稿写入(线程池:YAML/git 是同步 IO,别把它压进事件循环)。"""
    return await asyncio.to_thread(
        store.add, datasource, rule, source="scan", note=rule_note(finding))
