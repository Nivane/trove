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
    r"|依次|分别|先[^再。;；]{0,12}再|还要|以及|还有|及其|并|对比|比较"
    r"|TOP\s?\d+|排名|各(?:个|类|种|行|地|区域|行业)|每(?:个|家|类|种)"
    r"|另外[^同。;；]{0,12}同时|同时[^另。;；]{0,12}另外"
    r"|分\s*\S{0,4}\s*(?:项|步|部分)",
    re.DOTALL,
)

# LLM 判断层的弱提示词/长度阈值:规则未命中但"疑似多步"时才值得花一次
# LLM 调用(慢路径);短问句且无任何提示词 → 直接单任务,零 token。
JUDGE_MIN_LEN = 40
JUDGE_WEAK_HINTS = re.compile(r"其|以及|还有|对比|比较|TOP|排名|各|每")

# ContextPacket 文本化预算
ROWS_PREVIEW = 10      # _state_summary 携带的预览行数
CELL_CHAR_CAP = 40     # 预览单元格截断
PREVIEW_CAP = 5        # 注入 prompt 的预览行数
SQL_CAP = 400          # 注入 prompt 的 SQL 截断
PACKET_TEXT_CAP = 800  # [previous results] 块总长上限

# 跨轮任务操作的动作提示词(解释器触发门);未命中不调 LLM。
FOLLOWUP_HINTS = re.compile(
    r"继续|下一个|接着做|重做|跳过|第\s*[一二三四五六七八九十\d]+\s*(?:个|项|条|问)"
    r"|再来|换一个|剩余|还有几个|做完剩下的|再加|再添加",
)

_APPROVE_ALL = ("approve_all", "approveall", "ya", "2")


def looks_multitask(question: str) -> bool:
    """Cheap rule gate before spending an LLM decomposition call."""
    return bool(MULTITASK_HINTS.search(question))


def looks_likely_multitask(question: str) -> bool:
    """Second-tier gate: 规则未命中但"疑似多步"时值得花一次 LLM 判断。

    判据:问句较长(≥ JUDGE_MIN_LEN)或含弱提示词。误判(实际单任务)
    只浪费一次 fast 调用,正确性不受影响;短问句无提示词零成本。
    """
    text = question or ""
    return len(text) >= JUDGE_MIN_LEN or bool(JUDGE_WEAK_HINTS.search(text))


def cap_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= CELL_CHAR_CAP else text[:CELL_CHAR_CAP] + "…"


def format_result_packet(packet: dict) -> str:
    """ContextPacket → [previous results] 文本块(注入子任务/跨轮 prompt)。

    ``packet`` 是任务 metadata["context"] 里的字典;文本化带预算封顶
    (行数/单元格/总长),失败包带错误说明。
    """
    title = str(packet.get("title", "")).strip()
    sql = packet.get("sql")
    verdict = packet.get("verdict")
    error = packet.get("error")
    row_count = packet.get("row_count")
    lines = [
        "[previous results] 上一步结果:",
        f"- 问题: {title or '(未命名)'}",
    ]
    if sql:
        sql_text = sql if len(sql) <= SQL_CAP else sql[:SQL_CAP] + "…"
        lines.append(f"- SQL: {sql_text}")
    if verdict or error:
        lines.append(f"- 裁定: {error or verdict}")
    if row_count is not None and row_count >= 0:
        lines.append(f"- 行数: {row_count}")
    preview = (packet.get("rows_preview") or [])[:PREVIEW_CAP]
    if preview:
        rows = [" | ".join(cap_cell(v) for v in row) for row in preview]
        lines.append("- 预览: " + "  ;  ".join(rows))
    text = "\n".join(lines)
    return text if len(text) <= PACKET_TEXT_CAP else text[:PACKET_TEXT_CAP] + "…"


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
