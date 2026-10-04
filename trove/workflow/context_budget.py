"""Context budget assembly — priority-ordered prompt blocks with a token cap.

The gen_sql prompt has one mandatory core (question + matched schema)
and several optional blocks (few-shot examples, terminology, lessons,
plan, history). assemble_context fills the optional blocks by priority
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


def ref_label(text: str, limit: int = 60) -> str:
    """条目来源标识(装配 dump 的 ``ref`` 字段):归一化空白并截断。

    给"这条 item 来自哪"(示例问题/术语名/教训 pattern…)一个可读且稳定的
    标识;取不到来源的调用方留空,``assemble_context`` 的明细回退到 key。
    """
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


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
        ref: Source identifier for the assembly dump (KB 条目/术语/skill
            名等)。空 = 回退到 key(位置标识),见 assemble_context 的
            detail 报告。
    """

    key: str
    text: str
    score: float = 0.0
    ref: str = ""


def assemble_context(
    blocks: dict[str, list[ContextItem]],
    priorities: dict[str, int],
    budget_tokens: int,
    count: Callable[[str], int] = count_tokens,
    detail: bool = False,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Item-level context assembly within a global token budget.

    Items are filled GLOBALLY by effective score, ⑥ scale-unified:

    - per-block min-max normalization → [0, 1] (blocks whose scores sit
      on different scales — e.g. hybrid episodes ≈ 0.8-1.2 vs lexical
      0-1 — no longer let a systematically-high block crowd out another
      block's most relevant items);
    - priority weight ``1/priority`` multiplies the normalized score, so
      block priority stays the dominant lever while a highly-relevant
      item in a lower-priority block can still beat a weakly-relevant
      item in a higher-priority block (item-level relevance beats
      block-level all-or-nothing);
    - ties (equal effective score) break to the lower priority number.

    An item that does not fit the remaining budget is skipped (item-level
    trimming) instead of dropping the whole block. Blocks with no items
    are ignored. Usage report keeps the per-block shape (tokens/included/
    items_total/items_included) for observability.

    ``detail=True`` additionally reports, per block, ``truncated`` (有任
    一条被预算裁掉) and ``items`` — 逐项的 ``{ref, tokens, truncated}``,
    按块的源顺序排列、含被裁条目(它们的 ``tokens`` 是"本会花掉的成本")。
    这份明细是装配 dump 的 blocks 节;token 数取自填充循环已算的同一份
    计数(不重算),默认 False 保持既有报告形状(SSE / 前端契约)。

    Args:
        blocks: name → its items.
        priorities: name → priority (lower first; missing = lowest).
        budget_tokens: token cap for the assembled optional context.
        count: token estimator (default count_tokens — real tokenizer).
        detail: also report per-item cost/truncation (assembly dump).

    Returns:
        (included: {name: [kept item keys]}, usage report
        [{name, tokens, included, items_total, items_included}], plus
        ``truncated``/``items`` per entry when ``detail``).
    """
    ordered = sorted(blocks, key=lambda name: priorities.get(name, 100))
    # (effective_score, priority, block, item)——全局候选池
    scored: list[tuple[float, int, str, ContextItem]] = []
    for name in ordered:
        items = blocks[name]
        if not items:
            continue
        lo = min(it.score for it in items)
        hi = max(it.score for it in items)
        span = hi - lo
        prio = priorities.get(name, 100)
        weight = 1.0 / max(1, prio)
        for it in items:
            # span=0(整块同分,如 plan 0.0)→ 中性 0.5,块内无区分度
            norm = (it.score - lo) / span if span > 0 else 0.5
            scored.append((weight * norm, prio, name, it))
    scored.sort(key=lambda t: (-t[0], t[1]))

    used = 0
    included: dict[str, list[str]] = {}
    # id(item) -> 本条的 token 成本(填充循环对每条候选都算过一次;更新报告
    # 直接复用这一份,不再调 count 重算)。scored 里每个 item 对象仅出现一次。
    costs: dict[int, int] = {}
    for eff, prio, name, item in scored:
        cost = count(item.text)
        costs[id(item)] = cost
        if used + cost > budget_tokens:
            continue  # item-level trim: skip this item, try the next
        used += cost
        included.setdefault(name, []).append(item.key)

    usage: list[dict[str, Any]] = []
    for name in ordered:
        items = blocks.get(name) or []
        if not items:
            continue  # 空块不进报告(与填充前行为一致)
        kept = included.get(name, [])
        kept_set = set(kept)
        entry: dict[str, Any] = {
            "name": name,
            "tokens": sum(
                costs.get(id(it), 0) for it in items if it.key in kept_set
            ),
            "included": bool(kept),
            "items_total": len(items),
            "items_included": len(kept),
        }
        if detail:
            entry["truncated"] = len(kept) < len(items)
            entry["items"] = [
                {
                    "ref": it.ref or it.key,
                    "tokens": costs.get(id(it), 0),
                    "truncated": it.key not in kept_set,
                }
                for it in items
            ]
        usage.append(entry)
    return included, usage
