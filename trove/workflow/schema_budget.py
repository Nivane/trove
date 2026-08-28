"""Schema context budgeting for the gen prompt.

schema_linking's ``schema_context`` rendering can grow unbounded on wide
schemas and currently enters the gen prompt verbatim — the largest token
sink in the pipeline. This module splits the rendered schema into
per-table blocks plus a tail (value hints / semantic metrics / notes),
then trims to a token budget keeping the highest-signal tables first —
the schema-level counterpart of the item-level trimming in
``context_budget.assemble_context``.

Block recognition accepts both prefixes:
- ``Table: <name>`` — physical schema rendering (legacy / non-semantic path);
- ``Dataset: <name>`` — semantic-model rendering (semantic-first, Phase B).

Trimmed-away blocks are listed in a trailing note that points the model at
the ``lookup_schema`` tool, so it can lazily fetch the full DDL of any
dropped table on demand (agent can always reach the physical schema).

Trim is deterministic for a given (question, plan, budget), so the
stable cache prefix (dialect + schema) stays byte-identical across a
question's correction rounds.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from trove.workflow.context_budget import count_tokens
from trove.workflow.context_score import relevance_score

_BLOCK_RE = re.compile(r"^(?:Table|Dataset):\s*(\S+)", re.M)


def split_schema(
    schema_context: str,
) -> tuple[list[tuple[str, str]], str]:
    """把 schema_context 拆成 [(表/数据集名, 块), ...] 和尾部非表段。

    块以 ``Table: <name>`` 或 ``Dataset: <name>`` 开头(schema_linking 的
    两种渲染约定:物理 schema 与语义模型);其余段(Value hints / Semantic
    metrics / Semantic note)归入尾部,始终完整保留(跨表语义,信息密度
    高、体积小)。

    Returns:
        (blocks, tail) —— 保留原始顺序。
    """
    sections = re.split(r"\n\n+", (schema_context or "").strip())
    tables: list[tuple[str, str]] = []
    tail: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        m = _BLOCK_RE.match(sec)
        if m:
            tables.append((m.group(1), sec))
        else:
            tail.append(sec)
    return tables, "\n\n".join(tail)


def table_signal(
    name: str,
    block: str,
    query: str,
) -> float:
    """表块的保留信号:问句/计划里点名 + 与问句的词重叠。

    点名(bonus 2.0)远高于词重叠(0~1)——schema_linking 已按匹配度
    排序,这里只负责预算内的相对取舍,信号相同按原始顺序(排序稳定)。
    """
    signal = relevance_score(block, query)
    if name and name.lower() in (query or "").lower():
        signal += 2.0
    return signal


def trim_schema(
    schema_context: str,
    budget_tokens: int,
    question: str,
    plan_text: str = "",
    count: Callable[[str], int] = count_tokens,
) -> str:
    """预算内修剪 schema:保留信号最高的表/数据集块,尾部段始终保留。

    至少保留一张表;被裁掉的块在末尾列出,并提示用 lookup_schema 工具
    按需取回完整 DDL(agent 始终可触达物理 schema)。无表块(空/纯尾部
    schema)原样返回。

    Args:
        schema_context: schema_linking 的渲染输出。
        budget_tokens: 表/数据集块的 token 上限(尾部段不计入)。
        question: 当前问题(信号计算)。
        plan_text: 计划文本(信号计算,可选)。
        count: token 估算器。
    """
    tables, tail = split_schema(schema_context)
    if not tables:
        return schema_context
    query = f"{question or ''} {plan_text or ''}".strip()
    scored = [
        (name, block, table_signal(name, block, query))
        for name, block in tables
    ]
    ordered = sorted(scored, key=lambda s: s[2], reverse=True)
    used = 0
    kept: list[str] = []
    dropped: list[str] = []
    for name, block, _sig in ordered:
        cost = count(block)
        if kept and used + cost > budget_tokens:
            dropped.append(name)
            continue
        kept.append(block)
        used += cost

    parts = kept
    if tail:
        parts.append(tail)
    if dropped:
        parts.append(
            "[Additional tables in this datasource (fetch their schema "
            f"with the lookup_schema tool): {', '.join(dropped)}]"
        )
    return "\n\n".join(parts)


def trim_schema_for_state(
    state: Any,
    budget_tokens: int,
    count: Callable[[str], int] = count_tokens,
) -> str:
    """从 WorkflowState/GenSQLState 直接修剪 schema(复用 plan 文本)。

    便捷包装:plan 取自 state.plan(LLM 计划文本),问句取自
    state.question。
    """
    return trim_schema(
        state.schema_context,
        budget_tokens,
        state.question,
        plan_text=getattr(state, "plan", "") or "",
        count=count,
    )
