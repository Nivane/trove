"""Consensus selection node — execution-result voting across candidates.

Executes every candidate SQL, drops failures and rule-invalid results
(verify_step), then groups survivors by normalized result set. The
majority group wins; if the winner is not the primary candidate, its
result set is adopted in place (no LLM judge involved). A tie — every
group with a single vote, or a split majority — routes back to gen_sql
through the shared error_feedback channel with the concrete groups.

This is the deterministic counterpart to the LLM judge: execution
agreement is the strongest signal available without ground truth.

Order in the graph: execute_sql → select → validate → route.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.services.datasource.registry import ConnectorRegistry
from trove.workflow.rules import verify as run_rules
from trove.workflow.state import WorkflowState, budget_exhausted


def _normalize_rows(rows: list[list[Any]]) -> list[tuple[str, ...]]:
    """Set comparison: sorted, stringified rows (order/type insensitive)."""
    return sorted(tuple(str(v) for v in row) for row in rows)


def _compact_sql(sql: str, limit: int = 200) -> str:
    """Collapse whitespace and bound length — feedback embeds in prompts."""
    one = " ".join(sql.split())
    return one if len(one) <= limit else one[:limit] + "…"


def _preview_rows(rows: list[list[Any]], limit: int = 2, width: int = 40) -> str:
    """First rows' values, bounded — the concrete difference the model needs."""
    if not rows:
        return "[]"
    shown = [[str(v)[:width] for v in row] for row in rows[:limit]]
    return "[" + "; ".join(", ".join(r) for r in shown) + "]"


def make_select_consensus(
    connectors: ConnectorRegistry | None = None,
    timeout_ms: int = 30000,
    max_retries: int = 10,
    adopt_after_tie_rounds: int = 3,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the consensus select node.

    Args:
        connectors: Registry used to execute the candidate SQLs.
        timeout_ms: Timeout for each candidate execution.
        max_retries: Shared correction budget (same semantics as execute).
        adopt_after_tie_rounds: Tie rounds before adaptive degradation —
            when the pool has accumulated N rounds of votes without a
            majority, further regeneration has diminishing returns, so the
            strongest group is adopted with a low-confidence mark instead
            of burning the rest of the retry budget.

    Voting semantics (N candidates + 1 primary vote):
      - execution failures and rule-invalid candidates are dropped;
      - survivors group by normalized result set;
      - a unique majority (≥2 votes, strictly more than the runner-up)
        wins: the primary passes untouched if it is in the majority,
        otherwise the majority's SQL and result set are adopted;
      - any tie routes back to gen_sql with the concrete groups; at the
        retry cap the primary is delivered with a low-confidence mark.
    """

    async def select(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure / pending feedback / no candidates — pass through
        if state.error or state.error_feedback or not state.candidates:
            return {}
        if connectors is None:
            return {}

        async def run(cand: str):
            try:
                return await asyncio.wait_for(
                    connectors.execute(cand), timeout=timeout_ms / 1000.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return None

        qrs = await asyncio.gather(*[run(c) for c in state.candidates])

        # 1. verify_step 过滤(执行失败 / 结果不合规则的候选出局)
        groups: dict[tuple[tuple[str, ...], ...], list[tuple[str, Any]]] = {}
        filtered: list[dict[str, str]] = []
        for cand, qr in zip(state.candidates, qrs):
            if qr is None:
                filtered.append({"sql": _compact_sql(cand, 60), "reason": "execution-failed"})
                continue
            reason, _hits = run_rules(
                state.question, cand, qr.columns, qr.rows, qr.row_count,
                lang=state.lang,
            )
            if reason:
                filtered.append({"sql": _compact_sql(cand, 60), "reason": reason[:80]})
                continue
            groups.setdefault(tuple(_normalize_rows(qr.rows)), []).append((cand, qr))

        # primary 的票加入其所属分组(可能自成一票)
        primary_key = tuple(_normalize_rows(state.rows))
        if primary_key not in groups:
            groups[primary_key] = []
        groups[primary_key].insert(0, (state.sql, None))

        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        votes = {str(key): len(members) for key, members in ranked}
        if len(ranked) == 1:
            return {}  # 全员一致 — 高置信通过

        # 缺口5: 置信度 = 票王得票率(确定性,零 LLM)。
        # 全票 → 1.0;2:1 多数 → 2/3;平局 1:1:1 → 1/3 —— 降级/输出方观测用
        confidence = max(votes.values()) / sum(votes.values())

        top_key, top_members = ranked[0]
        runner_up_size = len(ranked[1][1])
        if len(top_members) >= 2 and len(top_members) > runner_up_size:
            # 唯一多数派胜出
            if top_key == primary_key:
                return {}  # 多数派就是主候选 — 高置信通过,无需纠正
            winner = next(m for m in top_members if m[1] is not None)
            return {
                "sql": winner[0],
                "columns": winner[1].columns,
                "rows": winner[1].rows,
                "row_count": winner[1].row_count,
                "consensus": True,
                "selection": {"votes": votes, "adopted": True,
                              "winner": "candidate", "filtered": filtered,
                              "confidence": confidence},
            }

        # 平局(全单票或并列) → 打回重生成
        if budget_exhausted(state.retry_count, max_retries):
            # Budget exhausted: deliver the primary with a low-confidence
            # mark instead of degrading to an error.
            return {"consensus": False, "selection": {"votes": votes,
                                                      "adopted": False,
                                                      "winner": "primary",
                                                      "filtered": filtered,
                                                      "degraded": "budget-exhausted",
                                                      "confidence": confidence}}
        if state.tie_rounds >= adopt_after_tie_rounds:
            # 自适应止损:平局 = 无唯一多数派(并列或全单票),没有"票王"可
            # 采纳——拉锯 N 轮仍无多数,继续打回收益递减,提前执行保守交付
            # (primary + 低置信标记),不再烧完共享 retry 预算(撞预算题形态)。
            return {"consensus": False, "selection": {"votes": votes,
                                                      "adopted": False,
                                                      "winner": "primary",
                                                      "filtered": filtered,
                                                      "degraded": "repeated-tie",
                                                      "confidence": confidence}}
        others = []
        for _key, members in ranked:
            sql, qr = members[0]
            if qr is None:
                continue  # primary 组(其 SQL 与结果已在主候选段给出)
            others.append(f"[{_compact_sql(sql)}] → {_preview_rows(qr.rows)}")
            if len(others) >= 2:
                break
        feedback = L(
            state.lang,
            (
                f"候选 SQL 结果不一致({len(votes)} 组,每组 {list(votes.values())} 票):"
                f"主候选 [{_compact_sql(state.sql)}] → {_preview_rows(state.rows)};"
                f"{'; '.join(others)}。"
                f"执行结果分组投票无法形成多数——选择最符合问题的解释并重新生成。"
            ),
            (
                f"Candidate SQL variants returned different results "
                f"({len(votes)} groups, votes {list(votes.values())}): "
                f"primary [{_compact_sql(state.sql)}] → {_preview_rows(state.rows)}; "
                f"{'; '.join(others)}. "
                f"Execution grouping produced no majority — choose the "
                f"interpretation that best matches the question and regenerate."
            ),
        )
        return {
            "error_feedback": feedback,
            "retry_count": state.retry_count + 1,
            "tie_rounds": state.tie_rounds + 1,  # 平局专用计数(自适应降级信号)
            "correction_history": [feedback],
            "consensus": False,
            "selection": {"votes": votes, "adopted": False,
                          "winner": "primary", "filtered": filtered,
                          "confidence": confidence},
        }

    return select
