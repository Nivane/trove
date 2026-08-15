"""User intent classification — route before the SQL pipeline.

Five intents:
  - QUERY: a data question → the NL2SQL pipeline
  - SCHEMA: questions about tables/columns ("有哪些表", "loan 有哪些字段")
  - SEMANTIC: questions about business term definitions/caliber
  - KNOWLEDGE: questions about the knowledge base contents
  - LINEAGE: questions about table relationships/origins

Classification is rule-based (zero cost); unclassified input defaults
to QUERY (the permissive path). An LLM classifier can be layered on
later for fuzzy phrasing.
"""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    QUERY = "query"
    SCHEMA = "schema"
    SEMANTIC = "semantic"
    KNOWLEDGE = "knowledge"
    LINEAGE = "lineage"


# 优先级从高到低：血缘 → 知识库 → 语义 → schema（更具体的问法优先）
_PATTERNS: list[tuple[Intent, list[str]]] = [
    (Intent.LINEAGE, [r"血缘", r"关联关系", r"数据来源", r"从哪.*(?:来|得到)", r"哪些表.*关联"]),
    (Intent.KNOWLEDGE, [r"知识库", r"参考\s*SQL", r"模板", r"示例", r"学过"]),
    (Intent.SEMANTIC, [r"口径", r"定义", r"含义", r"是什么意思"]),
    (Intent.SCHEMA, [r"有哪些表", r"表结构", r"字段", r"几张表", r"\btables\b", r"\bschema\b"]),
]

_COMPILED: list[tuple[Intent, list[re.Pattern]]] = [
    (intent, [re.compile(p, re.I) for p in patterns])
    for intent, patterns in _PATTERNS
]


def classify_intent(question: str) -> Intent:
    """Classify the user input into one of the five intents."""
    for intent, patterns in _COMPILED:
        for pattern in patterns:
            if pattern.search(question):
                return intent
    return Intent.QUERY
