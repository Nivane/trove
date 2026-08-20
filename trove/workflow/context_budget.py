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

Token counting is real-tokenizer-backed (tiktoken via TokenCounter)
for the budget assembly — the fixed 4-chars/token heuristic badly
undercounts CJK content (中文 ≈ 1.5-2 字符/token), which would silently
blow the budget on bilingual sessions. count_tokens falls back to a
CJK-aware character estimate when tiktoken is unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

# CJK / 全角字符集:中文、日文假名、韩文、全角标点按 ~2 字符/英文字符
# 的 token 当量加权(多数 tokenizer 下中文 1 字 ≈ 1.5-2 token)。
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\uff00-\uffef]"
)

_TOKENIZER: Any = None
try:
    from trove.llm.token_counter import TokenCounter

    _TOKENIZER = TokenCounter()
except Exception:  # pragma: no cover - import/encoding availability guard
    _TOKENIZER = None


def _char_estimate(text: str) -> int:
    """确定性字符估算(~4 字符/token,中文/全角按 2 字符加权),最小 1。

    纯文本、无依赖、可复现——与模板渲染对账及测试用;预��装配用
    count_tokens(真实分词)。
    """
    text = text or ""
    if not text:
        return 1
    n_cjk = len(_CJK_RE.findall(text))
    return max(1, (n_cjk * 2 + (len(text) - n_cjk)) // 4)


def count_tokens(text: str) -> int:
    """权威 token 估算:优先真实 tokenizer(tiktoken),退化到字符估算。

    中英混合内容按模型真实分词统计,避免固定 ``//4`` 对中文低估近一倍
    而让实际 prompt 超出预算。tiktoken 不可用(离线/encoding 缺失)时
    回到 ``_char_estimate``,行为确定。
    """
    global _TOKENIZER
    if _TOKENIZER is not None:
        try:
            return max(1, len(_TOKENIZER.encode(text or "")))
        except Exception:
            _TOKENIZER = None
    return _char_estimate(text)


def estimate_tokens(text: str) -> int:
    """轻量确定性 token 估算(``_char_estimate``),最小 1。

    与 count_tokens 的差异:estimate_tokens 不做分词、恒定 O(n)、结果
    可复现,适合 cache 前缀等纯长度对账;预算装配请用 count_tokens。
    """
    return _char_estimate(text)


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
    count: Callable[[str], int] = count_tokens,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Fill blocks by priority within the token budget.

    Args:
        blocks: name → rendered text.
        priorities: name → priority (lower first; missing = lowest).
        budget_tokens: token cap for the optional blocks.
        count: token estimator (default count_tokens — real tokenizer).

    Returns:
        (included names, usage report [{name, tokens, included}]).
    """
    ordered = sorted(blocks, key=lambda name: priorities.get(name, 100))
    used = 0
    included: set[str] = set()
    usage: list[dict[str, Any]] = []
    for name in ordered:
        cost = count(blocks[name])
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
    count: Callable[[str], int] = count_tokens,
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
        count: token estimator (default count_tokens — real tokenizer).

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
            cost = count(item.text)
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
