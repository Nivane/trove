"""User × datasource profile — correctness / failure modes / preferences.

P4: aggregates episodic history and committed facts into a per
(user, datasource) profile. Used for (a) the admin surface, and (b) an
optional generation hint when the same user repeatedly fails on a source
(the ``agent.memory.profile_boost`` switch) — never a hard gate.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from trove.services.memory.episode import _json_list


async def build_profile(
    episodes: Any, user_facts: Any | None,
    *,
    user_id: str, datasource: str = "",
) -> dict[str, Any]:
    """Aggregate one user's memory into a profile dict.

    Returns::

        {
          "user_id", "datasource",
          "totals": {"total", "ok", "empty", "error", "avg_retries"},
          "ok_rate": float,
          "failure_patterns": [{"pattern", "count"}],
          "recent_ok_questions": [...],
          "facts": [...],          # committed preference facts for this source
        }
    """
    scope_where = "WHERE user_id = ?" if not datasource else "WHERE user_id = ? AND datasource = ?"
    params: tuple[Any, ...] = (user_id,) if not datasource else (user_id, datasource)
    profile: dict[str, Any] = {
        "user_id": user_id,
        "datasource": datasource or "*",
        "totals": {"total": 0, "ok": 0, "empty": 0, "error": 0, "avg_retries": 0.0},
        "ok_rate": 0.0,
        "failure_patterns": [],
        "recent_ok_questions": [],
        "facts": [],
    }
    try:
        conn = await episodes._conn()
        try:
            cursor = await conn.execute(
                f"SELECT question, verdict, correction_history FROM episodes {scope_where} "
                "ORDER BY updated_at DESC LIMIT 500",
                params,
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
    except Exception:
        rows = []

    patterns: Counter = Counter()
    ok_questions: list[str] = []
    retries = 0
    for question, verdict, corr in rows:
        profile["totals"]["total"] += 1
        verdict = verdict or ""
        if verdict == "OK":
            profile["totals"]["ok"] += 1
            ok_questions.append(question)
        elif verdict == "EMPTY":
            profile["totals"]["empty"] += 1
        else:
            profile["totals"]["error"] += 1
        for reason in _json_list(corr):
            reason = str(reason).strip()
            if reason:
                patterns[reason[:120]] += 1
    total = profile["totals"]["total"]
    if total:
        profile["ok_rate"] = round(profile["totals"]["ok"] / total, 3)
    profile["failure_patterns"] = [
        {"pattern": p, "count": c} for p, c in patterns.most_common(5)
    ]
    profile["recent_ok_questions"] = ok_questions[:5]

    if user_facts is not None:
        try:
            profile["facts"] = await user_facts.list(user_id, datasource or None)
        except Exception:
            profile["facts"] = []
    return profile
