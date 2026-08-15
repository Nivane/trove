"""Context budget assembly — priority-ordered prompt blocks with a token cap.

The gen_sql prompt has one mandatory core (question + matched schema)
and several optional blocks (few-shot examples, terminology, lessons,
plan, history). assemble_blocks fills the optional blocks by priority
until the budget is spent and reports what was included — so the
pipeline stays bounded on large schemas and observability can show
exactly what context the model saw.
"""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token), minimum 1."""
    return max(1, len(text or "") // 4)


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
