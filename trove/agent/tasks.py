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
from collections.abc import Iterable

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
ROWS_PREVIEW = 10      # _state_summary 携带的预览行数(事件/接口载荷)
CELL_CHAR_CAP = 40     # 预览单元格截断
PREVIEW_CAP = 5        # 注入 prompt 的预览行数
SQL_CAP = 400          # 注入 prompt 的 SQL 截断
PACKET_TEXT_CAP = 10000  # [previous results] 块总长上限

# 结果通道(步骤间引用)的保真度预算。被引用的步骤是下一步的**输入**而不是
# 背景材料:它的 SQL 就是方法本身,它的行就是取值来源。按普通预览的
# 5 行 × 400 字符截,等于把通道截成了提示。
REF_ROWS_CAP = 30    # 物化被引用步骤时的预览行数
REF_SQL_CAP = 2000   # 物化时的 SQL 截断(落库 SQL 本身不截断)

# ContextPacket 落库保留的预览行数:比事件载荷(ROWS_PREVIEW=10)宽——
# 结果包是步骤间的数据通道,后续步骤引用 task[N] 时要真的有值可取。
# 单元格仍用 CELL_CHAR_CAP(与事件载荷/下载同源,不在这里单独放宽)。
PACKET_ROWS = 50

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


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "…"


def format_result_packet(packet: dict, *, ref: int | None = None, full: bool = False) -> str:
    """ContextPacket → 结果包文本(注入子任务/跨轮 prompt)。

    ``packet`` 是任务 metadata["context"] 里的字典;文本化带预算封顶
    (行数/单元格/总长),失败包带错误说明。

    ``ref`` 给结果包挂上 ``task[N]`` 句柄(结果通道的寻址名);
    ``full=True`` 换用 REF_* 预算——被引用的步骤是下一步的输入,
    按普通预览截会把通道截成提示。
    """
    title = str(packet.get("title", "")).strip()
    sql = packet.get("sql")
    verdict = packet.get("verdict")
    error = packet.get("error")
    row_count = packet.get("row_count")
    if ref is None:
        head = "[previous results] 上一步结果:"
    else:
        head = f"task[{ref}] {title or '(未命名)'}:"
    lines = [head]
    if ref is None:
        lines.append(f"- 问题: {title or '(未命名)'}")
    row_cap = REF_ROWS_CAP if full else PREVIEW_CAP
    if sql:
        lines.append(f"- SQL: {_clip(sql, REF_SQL_CAP if full else SQL_CAP)}")
    if verdict or error:
        lines.append(f"- 裁定: {error or verdict}")
    if row_count is not None and row_count >= 0:
        lines.append(f"- 行数: {row_count}")
    columns = list(packet.get("columns") or [])
    if full and columns:
        lines.append("- 列: " + " | ".join(str(c) for c in columns))
    preview = (packet.get("rows_preview") or [])[:row_cap]
    if preview:
        rows = [" | ".join(cap_cell(v) for v in row) for row in preview]
        lines.append("- 预览: " + "  ;  ".join(rows))
    return _clip("\n".join(lines), PACKET_TEXT_CAP)


# ── 结果通道:步骤间引用 ─────────────────────────────────────────────
# 已完成步骤的结果包不再只是一段「上一步如何」的叙述,而是可寻址的通道:
# [previous results] 给出每一步的 task[N] 句柄(清单),被引用的步骤按
# REF_* 预算物化(取值)。寻址是纯代码解析的,零 LLM——引用不命中就只是
# 少物化一步,不会错物化。
_REF_BRACKET = re.compile(r"task\s*\[\s*(\d+)\s*\]", re.IGNORECASE)
_REF_TASK = re.compile(r"任务\s*[\[(]?\s*(\d+)\s*[\])]?")
_REF_ORDINAL = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*(?:步|个|条|项|问|题|部分)")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(raw: str) -> int | None:
    """``一``/``十``/``十二``/``23`` → 整数;解析不出返回 None。"""
    text = raw.strip()
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text)


def parse_task_refs(text: str, total: int) -> list[int]:
    """子任务标题里对已完成步骤的显式引用 → 升序去重的 1-based 位置。

    确定性解析,零 LLM:``task[2]`` / ``任务2`` / ``第 2 步`` / ``第二步``。
    越界或解析不出的引用直接丢弃——宁可不物化,也不错物化。

    「上一步 / 前一步」不在这里解析:它是**默认**下钻锚点(最近一个已完成
    步骤),由 :func:`resolve_refs` 无条件物化,不需要标题显式命名。
    """
    if not text:
        return []
    found: set[int] = set()
    for pattern in (_REF_BRACKET, _REF_TASK):
        for raw in pattern.findall(text):
            found.add(int(raw))
    for raw in _REF_ORDINAL.findall(text):
        value = _cn_int(raw)
        if value is not None:
            found.add(value)
    return sorted(p for p in found if 1 <= p <= total)


def resolve_refs(title: str, pairs: list[tuple[int, dict]], total: int) -> list[int]:
    """本次要物化的步骤位置:默认下钻锚点 + 标题显式引用。

    默认锚点 = 最近一个已完成步骤(即 ``pairs`` 末位)——这是既有的
    「续问/下钻直接引用上一步结论」行为,保留;显式引用只能指向**已完成**
    的步骤(引用一个还没有结果包的步骤,无可物化)。
    """
    done = {pos for pos, _ in pairs}
    refs = {max(done)} if done else set()
    refs |= {p for p in parse_task_refs(title, total) if p in done}
    return sorted(refs)


def _packet_digest(packet: dict) -> str:
    """索引行:``学生名单 · 5 行 · OK``——够寻址,不占预算。"""
    title = str(packet.get("title", "")).strip() or "(未命名)"
    bits = [title]
    row_count = packet.get("row_count")
    if row_count is not None and row_count >= 0:
        bits.append(f"{row_count} 行")
    state = packet.get("error") or packet.get("verdict")
    if state:
        bits.append(str(state))
    return " · ".join(bits)


def format_previous_results(
    pairs: list[tuple[int, dict]],
    *,
    refs: Iterable[int] = (),
) -> str:
    """``[previous results]`` 块:全量句柄索引 + 被引用步骤的结果包。

    ``pairs`` 是 (1-based 位置, ContextPacket) 列表,按位置升序。
    ``refs`` 要按结果通道保真度物化的位置(不在 ``pairs`` 里的忽略)。

    索引让**每一步**都可寻址(此前只有最近一步在 prompt 里露面,更早的
    步骤一旦被下一步覆盖就再也回不来);物化只发生在被引用的步骤上——
    prompt 成本正比于实际用到的结果,而不是任务链的长度。
    """
    if not pairs:
        return ""
    ref_set = set(refs)
    lines = ["[previous results] 已完成步骤的结果(可用 task[N] 引用其中任一步):"]
    lines += [f"- task[{pos}] {_packet_digest(packet)}" for pos, packet in pairs]
    for pos, packet in pairs:
        if pos in ref_set:
            lines.append("")
            lines.append(format_result_packet(packet, ref=pos, full=True))
    return _clip("\n".join(lines), PACKET_TEXT_CAP)


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
