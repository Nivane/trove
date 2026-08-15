"""User intent classification — query vs metadata (two-way).

Metadata questions ask ABOUT the data itself (tables, calibers,
relationships, knowledge base); they are answered by a composite
metadata node that combines every signal found in the question —
no single-bucket routing.

Layers:
  1. Strong metadata signals route directly (zero cost);
  2. A bare "表" mention is a weak signal (could still be a data
     question) → the LLM confirms with a tiny two-way prompt;
  3. No signals → the permissive QUERY default.
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
    r"字段", r"列", r"指标", r"含义", r"意思", r"啥意思", r"干什么",
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


INTENT_PROMPT = """Classify the user input into ONE of: query | metadata.

- query: a data question to be answered with SQL
- metadata: a question ABOUT the data itself (tables, columns, business term definitions, table relationships, knowledge base contents)

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
