"""User intent classification — query vs metadata (two-way).

Metadata questions ask ABOUT the data itself (tables, calibers,
relationships, knowledge base); they are answered by a composite
metadata node that combines every signal found in the question —
no single-bucket routing.

Layers:
  1. The LLM classifies with a tiny two-way prompt (when available);
  2. The verdict is deterministically verified (see verify_intent):
     a METADATA verdict needs substance (strong signal / known table /
     known term), a QUERY verdict is overridden by a strong metadata
     signal without a data-question signal;
  3. LLM failure/unavailability falls back to regex: strong metadata
     signals route directly, otherwise the permissive QUERY default.
"""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    QUERY = "query"
    METADATA = "metadata"


# 强信号：高置信直接路由（零成本）
_STRONG_METADATA: list[str] = [
    r"有哪些表", r"表结构", r"几张表", r"\blist\s+tables\b", r"\btables\b",
    r"知识库", r"参考\s*SQL",
    r"血缘", r"数据来源",
    r"口径", r"定义", r"是什么意思",
]

# 弱信号：任何"元数据倾向词"→ 触发 LLM 二分类确认（不追求精确命中，
# 只保证不遗漏——精确的答案组织交给 LLM）
_WEAK: list[str] = [
    r"表", r"关系", r"关联", r"关连", r"连接", r"相连", r"怎么连",
    r"字段", r"列", r"指标", r"含义", r"意思", r"啥意思", r"是啥", r"是什么",
    r"干什么",
    r"模板", r"示例", r"结构", r"来源", r"术语", r"口径", r"定义",
    r"\btable\b", r"\bcolumn\b", r"\bschema\b", r"\bmetric\b",
    r"\bterm\b", r"\blineage\b", r"\bjoin\b", r"\blink\b",
]

_STRONG_COMPILED: list[re.Pattern] = [re.compile(p, re.I) for p in _STRONG_METADATA]
_WEAK_COMPILED: list[re.Pattern] = [re.compile(p, re.I) for p in _WEAK]


def classify_intent(question: str) -> Intent | None:
    """Strong-signal classification; None when no strong signal fires."""
    for pattern in _STRONG_COMPILED:
        if pattern.search(question):
            return Intent.METADATA
    return None


def has_weak_signal(question: str) -> bool:
    """Whether the input carries a weak metadata-ish signal (LLM confirms)."""
    return any(p.search(question) for p in _WEAK_COMPILED)


INTENT_PROMPT_ZH = """把用户输入分类为以下之一：query | metadata。

- query: 数据问题，需要用 SQL 回答
- metadata: 关于数据本身的问题（表、字段、业务术语定义、表关系、知识库内容）

例如「disp 是啥」「loan 表是什么」「平均贷款金额的定义」→ metadata。

只回答单个词。"""

INTENT_PROMPT = """Classify the user input into ONE of: query | metadata.

- query: a data question to be answered with SQL
- metadata: a question ABOUT the data itself (tables, columns, business term definitions, table relationships, knowledge base contents)

Examples: "what is the disp table?", "what does the loan table mean?" → metadata.

Reply with ONLY the single word."""


def parse_llm_intent(response: str) -> Intent | None:
    """Parse the tiny LLM classification reply into an Intent.

    Returns:
        Intent, or None when the reply is not a single valid token.
    """
    words = (response or "").strip().lower().split()
    token = words[0].strip(".,;:!?()[]{}\"'") if words else ""
    for intent in Intent:
        if token == intent.value:
            return intent
    return None


def verify_intent(
    llm_intent: Intent,
    *,
    strong_match: bool,
    mentioned_table: bool,
    term_hit: bool,
    data_signal: bool,
) -> Intent:
    """Deterministic verification of the LLM's intent verdict.

    Args:
        llm_intent: The LLM's two-way classification.
        strong_match: A strong metadata regex fired on the question.
        mentioned_table: The question mentions a known table (catalog hit).
        term_hit: The question hits a known business term (KB hit).
        data_signal: The question carries a data-question signal
            (count/list/percent/ordered patterns from workflow.rules).

    Returns:
        The verified intent. A METADATA verdict without any substance
        (no strong signal, no known table, no known term) falls back to
        the permissive QUERY default; a QUERY verdict is overridden to
        METADATA when the question is clearly about the data itself and
        carries no data-question signal.
    """
    if llm_intent == Intent.METADATA:
        if strong_match or mentioned_table or term_hit:
            return Intent.METADATA
        return Intent.QUERY
    if strong_match and not data_signal:
        return Intent.METADATA
    return Intent.QUERY
