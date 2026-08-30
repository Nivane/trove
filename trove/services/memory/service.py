"""Memory service facade — one front door over Trove's memory layers.

This is the P2 unification: instead of each caller reaching into KB /
user-facts / episodes / sessions / graph-state directly, the facade offers
one write/observe path (``observe``/``store``) and one read path
(``retrieve``), dispatching to per-kind providers while preserving the
repo's "deterministic gate decides return-or-not" retrieval philosophy.

Providers:
  - ``kb`` (KbService)         — terms / examples / lessons / rules (confirmed)
  - ``user_facts``             — per-user preference facts (committed)
  - ``episodes`` (EpisodeStore) — cross-session "what happened" recall
  - ``preferences`` (draft)    — auto-extracted prefs awaiting confirmation

New automatic memory (episodes/observations/preferences) writes *pending* or
self-contained entries only and never blocks queries on failure (every call
is best-effort, exceptions swallowed to the logger).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.memory.episode import EpisodeStore
from trove.services.memory.models import MemoryConfig, MemoryEntry, MemoryScope
from trove.services.memory.preferences import PreferenceStore

logger = get_logger(__name__)

# 本服务从同步的 episodes 等自动源收敛的 kind(KB/facts 由各自 provider 提供)。
_AUTO_KINDS = ("episode", "preference")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryService:
    """Unified memory front door (write/observe + read + lifecycle)."""

    def __init__(
        self,
        home: str | Path,
        config: MemoryConfig | None = None,
        *,
        kb: Any | None = None,
        user_facts: Any | None = None,
        llm: Any | None = None,
        connectors: Any | None = None,
        catalog: Any | None = None,
        config_resolver: Any | None = None,
    ):
        self.home = Path(home).expanduser()
        self.config = config or MemoryConfig()
        self.kb = kb
        self.user_facts = user_facts
        self.llm = llm
        self.connectors = connectors
        self.catalog = catalog
        # 数据源级配置(默认数据源 / retrieval 后端等),用于 schema_drift
        # 与生命周期。可空——缺失时相关能力静默降级。
        self.config_resolver = config_resolver
        self.episodes = EpisodeStore(self.home / "memory" / "episodes.sqlite")
        self.preferences = PreferenceStore(self.home / "memory" / "preferences.sqlite")

    # ── Public config helpers ─────────────────────────────

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def default_datasource(self) -> str:
        if self.connectors is not None:
            return getattr(self.connectors, "default_name", "") or ""
        return ""

    # ── Read (unified dispatch) ───────────────────────────

    async def retrieve(
        self,
        scope: MemoryScope,
        question: str,
        kinds: list[str] | None = None,
        limit: int = 3,
        *,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """Unified retrieval: each kind dispatches to its provider.

        Episodes are read-only-if-enabled; KB/facts delegate to their own
        providers (which apply the deterministic gate). Returns memory
        entries scored for the context-budget item-level trim.
        """
        if not self.enabled:
            return []
        kinds = kinds or ["episode"]
        out: list[MemoryEntry] = []
        for kind in kinds:
            if kind in ("episode", "episodes"):
                if self.config.episodes_enabled and scope.user_id:
                    out.extend(await self._retrieve_episodes(scope, question, limit))
            elif kind in ("fact", "facts") and self.user_facts is not None:
                out.extend(await self._retrieve_facts(scope, question, limit))
        out.sort(key=lambda e: e.score, reverse=True)
        return out[:limit]

    async def _retrieve_episodes(
        self, scope: MemoryScope, question: str, limit: int,
    ) -> list[MemoryEntry]:
        try:
            return await self.episodes.search(scope, question, limit=limit)
        except Exception as e:
            logger.warning("Episode retrieval failed (%s): %s", scope.datasource, e)
            return []

    async def _retrieve_facts(
        self, scope: MemoryScope, question: str, limit: int,
    ) -> list[MemoryEntry]:
        try:
            facts = await self.user_facts.search(
                scope.user_id, scope.datasource, question, limit=limit,
            )
        except Exception as e:
            logger.warning("User-fact retrieval failed (%s): %s", scope.datasource, e)
            return []
        return [
            MemoryEntry(
                kind="fact", scope=scope, content={"fact": f.get("fact", "")},
                source="manual", confidence=1.0, status="confirmed",
                score=0.0, updated_at=str(f.get("updated_at") or ""),
            )
            for f in facts
        ]

    # ── Write / observe (P1 feedback + P3 preferences) ────

    async def observe(
        self,
        *,
        scope: MemoryScope,
        session_id: str = "",
        run_id: str = "",
        question: str = "",
        sql: str = "",
        dialect: str = "",
        verdict: str = "",
        row_count: int = -1,
        result_signature: str = "",
        correction_history: list[str] | None = None,
        matched_tables: list[str] | None = None,
        error: str = "",
        evidence: str = "",
    ) -> None:
        """Automatic memory write-back after one query run.

        Paths:
          1. episode record (always, if enabled) — cross-session recall;
          2. success → pending reference example (auto_examples);
          3. correction → pending lesson with confidence (Hint Bank);
          4. failure → LLM-distilled pending lesson (opt-in cost path).
        Never raises: every failure is logged and skipped.
        """
        if not self.enabled:
            return
        scope = MemoryScope(
            datasource=scope.datasource or self.default_datasource(),
            user_id=scope.user_id,
        )
        if not scope.datasource:
            return
        try:
            if self.config.episodes_enabled:
                await self.episodes.record(
                    scope, session_id=session_id, run_id=run_id,
                    question=question, sql=sql, dialect=dialect,
                    verdict=verdict, row_count=row_count,
                    result_signature=result_signature,
                    correction_history=correction_history,
                    matched_tables=matched_tables,
                )
        except Exception as e:
            logger.warning("Episode record failed (%s): %s", scope.datasource, e)

        if self.config.examples_enabled and verdict in ("OK", "EMPTY") and sql:
            await self._draft_success_example(scope, question, sql)

        if correction_history:
            await self._capture_correction_lessons(scope, question, sql, correction_history)

        if error and not correction_history:
            await self._capture_failure_lesson(
                scope, question, sql, error, evidence)

    async def _draft_success_example(
        self, scope: MemoryScope, question: str, sql: str,
    ) -> None:
        """成功查询 → 待确认参考示例(pending, admin 确认后才可复用)。"""
        if self.kb is None:
            return
        try:
            await self.kb.draft_example(
                question, sql, scope.datasource,
                tags=["auto"], note="auto-captured successful query",
            )
        except Exception as e:
            logger.debug("Auto-example draft failed: %s", e)

    async def _capture_correction_lessons(
        self, scope: MemoryScope, question: str, sql: str,
        correction_history: list[str],
    ) -> None:
        """修正闭环理由 → pending 教训(带 confidence + source 字段)。"""
        if self.kb is None:
            return
        for reason in correction_history[-2:]:
            if not reason:
                continue
            try:
                await self.kb.append_lesson(
                    {
                        "pattern": reason[:120],
                        "note": reason[:200],
                        "sql_snippet": sql[:200],
                        "confidence": 0.5,
                        "source": "correction",
                        "evidence": f"question: {question[:120]}",
                    },
                    scope.datasource,
                )
            except Exception as e:
                logger.debug("Correction lesson capture failed: %s", e)

    async def _capture_failure_lesson(
        self, scope: MemoryScope, question: str, sql: str,
        error: str, evidence: str,
    ) -> None:
        """失败路径 → LLM 蒸馏教训(pending,静默降级)。"""
        if self.kb is None or self.llm is None:
            return
        from trove.services.kb.lesson_distill import (
            build_distill_prompt,
            is_noise_lesson,
            parse_lesson,
        )

        model = getattr(getattr(self.connectors, "config", None), "target", "") or "openai/gpt-4o"
        try:
            resp = await self.llm.chat(
                model=model,
                messages=[{
                    "role": "user",
                    "content": build_distill_prompt({
                        "question": question,
                        "evidence": evidence or "",
                        "gold_sql": "",
                        "pred_sql": sql or "",
                        "error": error,
                    }),
                }],
            )
        except Exception as e:
            logger.debug("Failure-lesson distill skipped: %s", e)
            return
        lesson = parse_lesson(resp)
        if not lesson or is_noise_lesson(question, lesson):
            return
        try:
            await self.kb.append_lesson(
                {
                    **lesson,
                    "confidence": 0.5,
                    "source": "auto_failure",
                    "evidence": f"question: {question[:120]}",
                },
                scope.datasource,
            )
        except Exception as e:
            logger.debug("Failure-lesson append failed: %s", e)

    async def store(
        self,
        scope: MemoryScope,
        content: dict[str, Any],
        *,
        kind: str,
        source: str = "auto",
        confidence: float = 0.0,
        status: str = "pending",
        idempotency_key: str = "",
    ) -> MemoryEntry | None:
        """Generic write entry point (delegates to the matching provider)."""
        if not self.enabled:
            return None
        if kind in ("episode", "episodes"):
            return None  # episodes have their own structured observe path
        if kind in ("preference", "preferences"):
            fact = str(content.get("fact") or "")
            if not fact:
                return None
            row = await self.preferences.add(
                scope, fact,
                evidence=str(content.get("evidence") or ""),
                confidence=confidence,
            )
            return MemoryEntry(
                kind="preference", scope=scope, content=row,
                source=source, confidence=confidence, status=status,
                idempotency_key=idempotency_key,
            )
        logger.debug("Memory store: unknown kind %r (ignored)", kind)
        return None

    async def touch(self, scope: MemoryScope, kind: str, idempotency_key: str) -> None:
        """Refresh last-used on read (lifecycle signal). Best-effort."""
        if not self.enabled:
            return
        try:
            if kind in ("episode", "episodes"):
                q, s = idempotency_key.split("\x1f", 1)
                await self.episodes.touch(scope, q, s)
        except Exception:
            pass

    # ── Preferences extraction (P3) ───────────────────────

    async def extract_preferences(
        self, scope: MemoryScope, conversation: str,
        model: str = "", lang: str = "zh",
    ) -> dict[str, Any]:
        """对话 → 偏好候选:高置信入 user_facts / 低置信落 pending 草稿。"""
        if not self.config.preferences_enabled or self.llm is None:
            return {"committed": [], "drafted": [], "skipped": []}
        from trove.services.memory.preferences import extract_and_store

        resolved_model = model or "openai/gpt-4o"
        return await extract_and_store(
            self.preferences, self.user_facts, self.llm, scope,
            conversation, resolved_model, lang=lang,
        )

    # ── Lifecycle (P2/P3/P4) ──────────────────────────────

    async def run_lifecycle(
        self, *, schema_check: bool | None = None, dry_run: bool = False,
    ) -> dict[str, Any]:
        """Periodic sweep: purge auto memory + user facts + retrieval log.

        Fixes the dead ``UserFactsService.purge_expired`` path (previously
        defined but never scheduled) and bounds the append-only retrieval
        log. Also runs optional schema-drift detection.
        """
        if not self.enabled:
            return {"skipped": True}
        stats: dict[str, Any] = {"purged": {}, "drift": {}}
        ret = self.config.retention_days
        try:
            n = await self.episodes.purge(ret.get("episodes"))
            stats["purged"]["episodes"] = n
        except Exception as e:
            logger.warning("Episode purge failed: %s", e)
        try:
            n = await self.preferences.purge(ret.get("preferences"))
            stats["purged"]["preferences"] = n
        except Exception as e:
            logger.warning("Preference purge failed: %s", e)
        if self.user_facts is not None and ret.get("facts"):
            try:
                n = await self.user_facts.purge_expired(int(ret["facts"]))
                stats["purged"]["facts"] = n
            except Exception as e:
                logger.warning("User-fact purge failed: %s", e)
        if ret.get("retrieval_log"):
            try:
                n = await self._purge_retrieval_log(int(ret["retrieval_log"]))
                stats["purged"]["retrieval_log"] = n
            except Exception as e:
                logger.warning("Retrieval-log purge failed: %s", e)
        if (schema_check if schema_check is not None else self.config.schema_drift_check):
            try:
                stats["drift"] = await self.detect_schema_drift()
            except Exception as e:
                logger.warning("Schema-drift check failed: %s", e)
        return stats

    async def _purge_retrieval_log(self, retention_days: int) -> int:
        """Bound the append-only retrieval query log (best-effort)."""
        from datetime import timedelta

        from trove.services.retrieval.query_log import QueryLogRecorder

        path = self.home / "retrieval" / "query_log.sqlite"
        rec = QueryLogRecorder(path)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        try:
            await rec._ensure()
            cursor = await rec._backend.execute(
                "DELETE FROM retrieval_log WHERE ts < ?", (cutoff,))
            await rec._backend.commit()
            return cursor.rowcount
        except Exception:
            return 0

    async def detect_schema_drift(self) -> dict[str, Any]:
        """Live schema vs KB schema_notes drift (zero LLM)."""
        if self.kb is None or self.catalog is None:
            return {"skipped": True, "reason": "kb/catalog unavailable"}
        from trove.services.memory.schema_drift import detect_drift

        reports: dict[str, Any] = {}
        for ds in self._datasource_names():
            try:
                report = await detect_drift(ds, self.kb, self.catalog)
                if report["new_tables"] or report["gone_tables"] or report["column_changes"]:
                    reports[ds] = report
            except Exception as e:
                logger.warning("Drift check failed for %s: %s", ds, e)
        return {"datasources": reports}

    def _datasource_names(self) -> list[str]:
        if self.config_resolver is not None:
            try:
                cfgs = self.config_resolver.load_configs()
                return [getattr(c, "name", "") for c in cfgs if getattr(c, "name", "")]
            except Exception:
                return []
        if self.connectors is not None:
            try:
                return list(self.connectors.list_names())
            except Exception:
                return []
        default = self.default_datasource()
        return [default] if default else []

    # ── Promotion (P3) ────────────────────────────────────

    async def promote_lesson(
        self, datasource: str, pattern: str,
        *, evidence_kind: str = "repeated_correction", count: int = 1,
    ) -> dict[str, Any]:
        """Bump a pending lesson's confidence; auto-confirm past threshold."""
        if not self.config.promotion_enabled or self.kb is None:
            return {"promoted": False, "reason": "promotion disabled"}
        try:
            return await self.kb.update_lesson_confidence(
                datasource, pattern,
                evidence_kind=evidence_kind, count=count,
                threshold=self.config.promotion_threshold,
            )
        except Exception as e:
            logger.warning("Lesson promotion failed: %s", e)
            return {"promoted": False, "reason": str(e)}

    # ── Profile (P4) ──────────────────────────────────────

    async def profile(
        self, user_id: str, datasource: str = "",
    ) -> dict[str, Any]:
        """用户×数据源画像:正确率 / 失败模式 / 偏好。"""
        from trove.services.memory.profile import build_profile

        return await build_profile(
            self.episodes, self.user_facts,
            user_id=user_id, datasource=datasource,
        )
