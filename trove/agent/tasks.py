"""Task coordination helpers — pure, deterministic parts of the task layer.

``SessionManager`` owns the orchestration (rule gate → LLM decomposition →
sequential execution → cross-turn follow-up interpretation). This module
keeps the deterministic pieces (hint rules, JSON parsing) testable without
LLM mocks, mirroring how ``workflow/context_score.py`` etc. factor out
pure logic from node factories.
"""

from __future__ import annotations

import json
import re

# 多任务指令的廉价规则预检:命中才花一次 LLM 拆解调用;
# 未命中(绝大多数单问题)走原路径,零额外 token。
MULTITASK_HINTS = re.compile(
    r"(\d+)\s*[\.、．)）]"
    r"|[一二三四五六七八九十]{1,3}\s*[、．,，]"
    r"|依次|分别|先[^再。;；]{0,12}再|还要|以及|另外[^同。;；]{0,12}同时|同时[^另。;；]{0,12}另外"
    r"|分\s*\S{0,4}\s*(?:项|步|部分)",
    re.DOTALL,
)

# 跨轮任务操作的动作提示词(解释器触发门);未命中不调 LLM。
FOLLOWUP_HINTS = re.compile(
    r"继续|下一个|接着做|重做|跳过|第\s*[一二三四五六七八九十\d]+\s*(?:个|项|条|问)"
    r"|再来|换一个|剩余|还有几个|做完剩下的|再加|再添加",
)

_APPROVE_ALL = ("approve_all", "approveall", "ya", "2")


def looks_multitask(question: str) -> bool:
    """Cheap rule gate before spending an LLM decomposition call."""
    return bool(MULTITASK_HINTS.search(question))


def looks_task_followup(question: str) -> bool:
    """Cheap rule gate for the cross-turn interpreter."""
    return bool(FOLLOWUP_HINTS.search(question))


def is_approve_all(decision: object) -> bool:
    """True when the HITL resume decision approves the whole batch."""
    return isinstance(decision, str) and decision.strip().lower() in _APPROVE_ALL


def is_reject(decision: object) -> bool:
    """True when the HITL resume decision rejects the proposal."""
    if isinstance(decision, str):
        return decision.strip().lower() in ("reject", "no", "n", "cancel", "0", "false", "3")
    return decision is False


def parse_task_json(text: str) -> list[str]:
    """Parse the decomposition LLM response into sub-question titles.

    Tolerates ```json fences and stray prose around the first ``{...}``
    block. Returns ``[]`` when nothing parseable — callers degrade to the
    single-task path.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return []
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [str(t).strip() for t in tasks if str(t).strip()]


def parse_action_json(text: str) -> dict:
    """Parse the follow-up interpreter response into an action dict.

    Valid shapes: ``{"action": "continue_next"|"redo"|"skip"|"add"|"none",
    "index": int}``. Anything else degrades to ``{"action": "none"}``.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return {"action": "none"}
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {"action": "none"}
    action = data.get("action", "none")
    if action not in {"continue_next", "redo", "skip", "add", "none"}:
        return {"action": "none"}
    out: dict = {"action": action}
    if isinstance(data.get("index"), int) and not isinstance(data.get("index"), bool):
        out["index"] = data["index"]
    return out
