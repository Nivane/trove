"""Eval 失败 → Hint Bank lesson 蒸馏(冷启动经验批量制造)。

从 eval 失败记录(question/gold_sql/pred_sql/error)提炼可复用教训:
pattern 取问题里的判别短语(检索时需命中问题原文,见 kb.search_lessons
的子串匹配),note 是教训本身。scripts/distill_lessons.py 负责批量调用。
"""

from __future__ import annotations

import json
import re
from typing import Any

DISTILL_SYSTEM = (
    "You distill reusable lessons from Text2SQL evaluation failures. "
    'For each failure, output ONLY a JSON object: '
    '{"pattern": "...", "note": "...", "sql_snippet": "..."}\n'
    "- pattern: a distinctive 2-6 word phrase from the question (verbatim, "
    "lowercase) that future questions with the same pitfall will contain; "
    "- note: the concrete lesson — what was wrong and how to fix it; "
    "- sql_snippet: a short fragment of the WRONG SQL (up to 120 chars)."
)


def build_distill_prompt(failure: dict[str, Any]) -> str:
    """失败记录 → 蒸馏提示词(含 gold 与 pred 对照)。"""
    return "\n".join([
        "A Text2SQL question was answered incorrectly.",
        "",
        f"Question: {failure.get('question', '')}",
        f"Evidence: {failure.get('evidence', '')}",
        f"Gold SQL: {failure.get('gold_sql', '')}",
        f"Predicted SQL: {failure.get('pred_sql', '')}",
        f"Error: {failure.get('error', '')}",
        "",
        "Extract the reusable lesson.",
    ])


def parse_lesson(response: str) -> dict[str, str] | None:
    """LLM 回复 → lesson dict;非 JSON 或缺 pattern/note 返回 None。"""
    text = (response or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pattern = str(data.get("pattern", "")).strip()
    note = str(data.get("note", "")).strip()
    if not pattern or not note:
        return None
    return {
        "pattern": pattern[:120],
        "note": note[:300],
        "sql_snippet": str(data.get("sql_snippet", "")).strip()[:200],
    }


def dedupe_by_pattern(
    entries: list[dict[str, Any]], existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 pattern 去重(大小写不敏感):已有教训与批内重复都跳过。"""
    seen = {
        str(l.get("pattern", "")).strip().lower()
        for l in existing if str(l.get("pattern", "")).strip()
    }
    fresh: list[dict[str, Any]] = []
    for e in entries:
        p = str(e.get("pattern", "")).strip().lower()
        if p and p not in seen:
            fresh.append(e)
            seen.add(p)
    return fresh
