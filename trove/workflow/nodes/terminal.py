"""Terminal intent answers — zero-LLM direct responses.

Routed from route_intent for non-data intents:
  - answer_reject:     write intent — polite refusal (Trove is read-only)
  - answer_chitchat:   greeting / thanks / goodbye / capability canned replies
  - answer_correction: pure feedback with no history to re-run — guidance

Node shape: `async def answer_*(state: WorkflowState) -> dict`
returns a partial state update ({"intent_answer": ...}); the output
node delivers it as final_response.
"""

from __future__ import annotations

from typing import Any

from trove.core.i18n import L
from trove.workflow.intent import chitchat_subtype
from trove.workflow.state import WorkflowState

# 闲聊话术按子类(zh, en);other = 兜底能力简介
_CHITCHAT_TEXT: dict[str, tuple[str, str]] = {
    "greet": (
        "你好!我是 Trove,数据问答助手。你可以用自然语言问我数据问题,"
        "比如「哪个地区的平均贷款金额最高?」。",
        "Hello! I'm Trove, a data Q&A assistant. Ask me anything about your data in "
        "natural language, e.g. \"Which region has the highest average loan amount?\"",
    ),
    "thanks": (
        "不客气!还有什么数据问题想查吗?",
        "You're welcome! Anything else you'd like to query?",
    ),
    "bye": (
        "再见,随时回来问我数据问题!",
        "Goodbye — come back anytime for your data questions!",
    ),
    "capability": (
        "我是 Trove,一个 NL→SQL 数据分析助手:你用自然语言提问,我生成 SQL、"
        "执行并返回结果。试试「哪些地区的贷款金额最高?」。",
        "I'm Trove, an NL→SQL data assistant: you ask in natural language, I generate "
        "and run the SQL, and return the results. Try \"Which region has the highest "
        "loan amounts?\"",
    ),
    "other": (
        "你好!我是 Trove,数据问答助手。告诉我你想了解什么数据,"
        "我会生成查询并回答。",
        "Hi! I'm Trove, a data Q&A assistant. Tell me what data you'd like to know "
        "about and I'll query it for you.",
    ),
}


async def answer_reject(state: WorkflowState) -> dict[str, Any]:
    """Write intent → polite refusal (read-only tool)."""
    return {"intent_answer": L(
        state.lang,
        "Trove 是只读数据分析助手,不支持增删改等写操作。"
        "我可以帮你查询和分析数据,比如「哪个地区的平均贷款金额最高?」。",
        "Trove is a read-only data assistant — write operations "
        "(insert/update/delete/DDL) are not supported. I can query and analyze "
        "data, e.g. \"Which region has the highest average loan amount?\"",
    )}


async def answer_chitchat(state: WorkflowState) -> dict[str, Any]:
    """Canned chitchat reply by subtype (greet/thanks/bye/capability/other)."""
    zh, en = _CHITCHAT_TEXT.get(chitchat_subtype(state.question), _CHITCHAT_TEXT["other"])
    return {"intent_answer": L(state.lang, zh, en)}


async def answer_correction(state: WorkflowState) -> dict[str, Any]:
    """Pure feedback with nothing to re-run → guidance."""
    return {"intent_answer": L(
        state.lang,
        "收到。如果上一问的结果不对,直接说「重算」我会重新跑一遍;"
        "也可以直接补充要修改的内容,比如「不对,用日均余额口径重算」。",
        "Got it. If the previous answer looked wrong, say \"recalculate\" and I'll "
        "re-run it; or tell me what to change, e.g. \"no, recalculate with the "
        "average daily balance caliber\".",
    )}
