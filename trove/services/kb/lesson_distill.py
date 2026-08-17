"""Eval 失败 → Hint Bank lesson 蒸馏(冷启动经验批量制造)。

从 eval 失败记录(question/gold_sql/pred_sql/error)提炼可复用教训:
pattern 取问题里的判别短语(检索时需命中问题原文,见 kb.search_lessons
的子串匹配),note 是教训本身。scripts/distill_lessons.py 负责批量调用。
"""

from __future__ import annotations

import json
import re
from typing import Any

from trove.prompts import render


def build_distill_prompt(failure: dict[str, Any]) -> str:
    """失败记录 → 蒸馏提示词(含 gold 与 pred 对照)。"""
    return render(
        "lesson_distill/user",
        question=failure.get("question", ""),
        evidence=failure.get("evidence", ""),
        gold_sql=failure.get("gold_sql", ""),
        pred_sql=failure.get("pred_sql", ""),
        error=failure.get("error", ""),
    )


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


# 管线噪声句式:一致性拉锯、语法错误、引擎报错——不是可复用教训
_ERROR_PATTERN_RE = re.compile(
    r"(\[sql_|sql syntax|syntax error|execution error|candidate sql variants"
    r"|mysql|sqlite|unstable)",
    re.IGNORECASE,
)

MAX_PATTERN_LEN = 40


def is_noise_lesson(question: str, lesson: dict[str, Any]) -> bool:
    """管线噪声判断:pattern 必须是问题原文中的短判别短语。

    蒸馏提示词约定 pattern 取问题里 2-6 词的原文短语;一致性拉锯
    ("variants returned different results")与语法错误记录会把整句错误
    文本写成 pattern,永远命中不了新问题,还占 Hint Bank 配额。
    """
    pattern = str(lesson.get("pattern", "") or "").strip()
    note = str(lesson.get("note", "") or "").strip()
    p = pattern.lower()
    if not p:
        return True
    if len(p) > MAX_PATTERN_LEN:
        return True
    if _ERROR_PATTERN_RE.search(p):
        return True
    if p not in (question or "").lower():
        return True
    if note.lower() == p:
        return True
    return False
