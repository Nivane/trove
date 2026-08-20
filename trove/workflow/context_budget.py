"""Context budget assembly — priority-ordered prompt blocks with a token cap.

The gen_sql prompt has one mandatory core (question + matched schema)
and several optional blocks (few-shot examples, terminology, lessons,
plan, history). assemble_blocks fills the optional blocks by priority
until the budget is spent and reports what was included — so the
pipeline stays bounded on large schemas and observability can show
exactly what context the model saw.

assemble_context is the item-level variant: instead of dropping a whole
block when it does not fit, each block's items are scored and filled in
score-descending order, so a block keeps its most relevant items under
a tight budget rather than all-or-nothing (industry practice: budget on
the item/segment level, LLMLingua-style, instead of the block level).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token), minimum 1."""
    return max(1, len(text or "") // 4)


@dataclass
class ContextItem:
    """One renderable, scoreable unit within a context block.

    Args:
        key: Stable identifier, used to filter the source list back
            after assembly (callers keep item.key → source item).
        text: Rendered text of this single item — must match the
            template's per-item format so the estimate mirrors the
            prompt (format drift inflates/deflates the estimate).
        score: Selection priority within the block (higher filled first).
            Items without a real relevance signal use 0.0 and keep their
            retrieval order (Python's sort is stable).
    """

    key: str
    text: str
    score: float = 0.0


def assemble_blocks(
    blocks: dict[str, str],
    priorities: dict[str, int],
    budget_tokens: int,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Fill blocks by priority within the token budget.

    Args:
        blocks: name → rendered text.
        priorities: name → priority (lower first; missing = lowest).
        budget_tokens: token cap for the optional blocks.

    Returns:
        (included names, usage report [{name, tokens, included}]).
    """
    ordered = sorted(blocks, key=lambda name: priorities.get(name, 100))
    used = 0
    included: set[str] = set()
    usage: list[dict[str, Any]] = []
    for name in ordered:
        cost = estimate_tokens(blocks[name])
        if used + cost > budget_tokens:
            usage.append({"name": name, "tokens": cost, "included": False})
            continue
        used += cost
        included.add(name)
        usage.append({"name": name, "tokens": cost, "included": True})
    return included, usage


def assemble_context(
    blocks: dict[str, list[ContextItem]],
    priorities: dict[str, int],
    budget_tokens: int,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Item-level context assembly within a global token budget.

    Blocks are visited in priority order (lower priority number first).
    Within each block, items are filled in score-descending order; an
    item that does not fit the remaining budget is skipped (item-level
    trimming) instead of dropping the whole block — a block keeps its
    most relevant items rather than all-or-nothing. Blocks with no
    items are ignored.

    Args:
        blocks: name → its items.
        priorities: name → priority (lower first; missing = lowest).
        budget_tokens: token cap for the assembled optional context.

    Returns:
        (included: {name: [kept item keys]}, usage report
        [{name, tokens, included, items_total, items_included}]).
    """
    ordered = sorted(blocks, key=lambda name: priorities.get(name, 100))
    used = 0
    included: dict[str, list[str]] = {}
    usage: list[dict[str, Any]] = []
    for name in ordered:
        items = blocks[name]
        if not items:
            continue
        block_used = 0
        kept: list[str] = []
        for item in sorted(items, key=lambda it: it.score, reverse=True):
            cost = estimate_tokens(item.text)
            if used + block_used + cost > budget_tokens:
                continue  # item-level trim: skip this item, try the next
            block_used += cost
            kept.append(item.key)
        if kept:
            used += block_used
            included[name] = kept
        usage.append({
            "name": name,
            "tokens": block_used,
            "included": bool(kept),
            "items_total": len(items),
            "items_included": len(kept),
        })
    return included, usage
