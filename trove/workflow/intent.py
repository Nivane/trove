"""User intent classification — five-way routing.

Intents:
  - query:     a data question to be answered with SQL
  - metadata:  a question ABOUT the data itself (tables, calibers,
               relationships, knowledge base)
  - write:     a request to modify data (insert/delete/update/DDL) —
               Trove is read-only, these are refused (safety)
  - chitchat:  greeting / thanks / goodbye / capability questions —
               answered with canned text, zero LLM
  - correction: feedback on the previous answer (pure feedback →
               re-run the previous question) or an elliptical follow-up
               ("那北京呢?") → rewritten with history then routed onward
  - attribution: a "why / contribution / root-cause" business question
               ("为什么营收下降"、"哪个地区贡献最大") — still walks the
               query main chain (parse_date → schema_linking → ...), but
               downstream query_sketch/attribution nodes act on the
               attribution plan instead of plain SQL generation

Layers:
  1. The LLM classifies with a tiny five-way prompt (when available);
  2. The verdict is deterministically verified (see verify_intent);
  3. LLM failure/unavailability falls back to regex: strong signals
     route directly, otherwise the permissive QUERY default.

Write-signal detection is deliberately high-precision (never refuse a
legitimate read question): it requires a mutation verb plus a
data-ish object, and avoids common field-name collisions
(创建时间/更新时间) and filter contexts (被删除). The execution layer
carries the real write protection (see ConnectorRegistry.execute);
this layer is best-effort refusal UX.
"""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    QUERY = "query"
    METADATA = "metadata"
    WRITE = "write"
    CHITCHAT = "chitchat"
    CORRECTION = "correction"
    CONFIRM = "confirm"
    ATTRIBUTION = "attribution"


# ── 既有信号:metadata 强/弱(原样保留)──────────────────────────

# 强信号:高置信直接路由(零成本)
# 注意「口径」是语境信号:「用X口径重算」是纠正实质(走 query),
# 只有「询问口径」(口径是什么 / X口径的定义 / …口径$ )才算 metadata。
_STRONG_METADATA: list[str] = [
    r"有哪些表", r"表结构", r"几张表", r"\blist\s+tables\b", r"\btables\b",
    r"知识库", r"参考\s*SQL",
    r"血缘", r"数据来源", r"元数据", r"\bmetadata\b",
    r"口径(?:是|为|指)\s*(?:什么|啥)?|(?:的)?口径\s*$|口径(?:的定义|的含义|是什么意思)",
    r"定义", r"是什么意思",
]

# 弱信号:任何"元数据倾向词"→ 触发 LLM 二分类确认(不追求精确命中,
# 只保证不遗漏——精确的答案组织交给 LLM)
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


# ── 写意图:动词 + 数据对象(高精度,宁漏勿误拒)────────────────

_WRITE_OBJECTS = (
    r"记录|数据|表|行|内容|客户|账户|额度|余额|金额|贷款|订单|用户|项"
)
_WRITE_VERBS = (
    r"删除|删掉|清空|清除|插入|新增|更新|修改|创建|建立|重命名|改名|增删改查"
)
# 动词在前,对象在后:删掉重复记录 / 修改loan表
_WRITE_VERB_OBJECT = re.compile(
    rf"(?<!被)(?:{_WRITE_VERBS}).{{0,8}}(?:{_WRITE_OBJECTS})", re.I
)
# 对象在前,动词在后(把字句):把重复记录删掉 / 把数据清空
_WRITE_OBJECT_VERB = re.compile(
    rf"(?:{_WRITE_OBJECTS}).{{0,4}}(?:删除|删掉|清空|清除)", re.I
)
# 显式 DDL / 管理语句
_WRITE_DDL = re.compile(
    r"(?:建表|增删改查|\b(?:drop|create|alter|truncate)\s+table\b|\bgrant\b|\brevoke\b)",
    re.I,
)
# 英文动词 + 目标(要求空白分隔,避免 update_time 之类字段名误报)
_WRITE_EN = re.compile(
    r"\b(?:insert|delete|update|drop|alter|truncate|create)\b\s+"
    r"(?:the|a|an|from|into|table|data|row|record|account|loan|customer|amount)",
    re.I,
)


def has_strong_write(question: str) -> bool:
    """写意图强信号:命中即安全拒绝(路由层 + 执行层双保险)。"""
    return bool(
        _WRITE_VERB_OBJECT.search(question)
        or _WRITE_OBJECT_VERB.search(question)
        or _WRITE_DDL.search(question)
        or _WRITE_EN.search(question)
    )


# ── 闲聊:纯寒暄才直接路由(带数据词 → 不是闲聊)──────────────

_CHITCHAT_PATTERNS: list[tuple[str, str]] = [
    (r"^(?:你好|您好|嗨|哈喽|hello|hi)\b", "greet"),
    (r"^(?:谢谢|感谢|多谢|thanks|thank you)\b", "thanks"),
    (r"^(?:再见|拜拜|bye)\b", "bye"),
    (r"(?:你是谁|你能做什么|你会什么|你能干什么|怎么用|怎么使用|"
     r"who are you|what can you do)", "capability"),
    (r"^(?:你呢|你好吗|你怎么样)\b", "greet"),
]
_CHITCHAT_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(p, re.I), t) for p, t in _CHITCHAT_PATTERNS
]
# 数据倾向词:命中说明问的是数据,不是寒暄
_CHITCHAT_DATA_WORDS = re.compile(
    r"多少|最高|最低|最大|最小|哪个|哪些|统计|平均|占比|数据|表|列|字段|"
    r"金额|额度|客户|贷款|账户|订单|人数|数量|"
    r"account|loan|customer|amount|table|data|row|column|how many|count",
    re.I,
)


def has_strong_chitchat(question: str) -> bool:
    """纯闲聊强信号:命中寒暄模板且不带数据倾向词。"""
    if not question.strip():
        return False
    if _CHITCHAT_DATA_WORDS.search(question):
        return False
    return any(p.search(question) for p, _ in _CHITCHAT_COMPILED)


def chitchat_subtype(question: str) -> str:
    """闲聊子类:greet / thanks / bye / capability / other(选话术用)。"""
    for pattern, subtype in _CHITCHAT_COMPILED:
        if pattern.search(question):
            return subtype
    return "other"


# ── 纠正/追问:纯反馈重跑上一问,省略式追问补全重路由──────────

_CORRECTION_PATTERNS = [
    r"^(?:不对|错了|不对吧|错了吧|算错)",
    r"(?:重算|重跑|再算一次|重新算|重新计算|再来一次)",
    r"(?:结果不对|答案不对|数字错了|数不对)",
    r"^(?:recalculate|rerun|recompute|wrong)\b",
    r"(?:wrong answer|that'?s (?:not )?right)",
]
_CORRECTION_COMPILED: list[re.Pattern] = [
    re.compile(p, re.I) for p in _CORRECTION_PATTERNS
]
# 实质内容词:带这些词就不是"纯反馈",而是含新数据的纠正(走正常 query)
_CORRECTION_SUBSTANCE = re.compile(
    r"应该|要|用|换成|改成|口径|范围|时间|日期|金额|额度|客户|贷款|账户|订单|"
    r"数据|数量|多少|哪个|哪些|最高|最低|平均|占比|统计|名字|字段|表|列|"
    r"account|loan|customer|amount|data|table",
    re.I,
)

_REFERENTIAL = re.compile(r"那|这|它|这些|那些|其|刚才|上一(?:次|问)|上面|以下")
_FOLLOWUP_TAIL = re.compile(r"(?:呢|吗)\s*$")


def has_strong_correction(question: str) -> bool:
    """纯反馈强信号:命中纠正词且不含数据实质。"""
    if not any(p.search(question) for p in _CORRECTION_COMPILED):
        return False
    return not _CORRECTION_SUBSTANCE.search(question)


# ── 归因:为什么/贡献/驱动类业务问题(走 query 主链,产出归因计划)──────

# 触发词:为什么 / 归因 / 贡献 / 根因 / 驱动 等。单独命中不足以判归因——
# 「为什么天是蓝的」是 chitchat,必须同时命中数据词(变化词或业务指标词),
# 保证"有明确 metric/实体"才进入归因(设计文档的 data_signal 门禁)。
_ATTRIBUTION_TRIGGER = re.compile(
    r"为什么|为何|归因|根因|贡献|主要(?:原因|因素|影响|来自)|导致|驱动|由于|"
    r"\bwhy\b|\bdue to\b|\bdriven by\b|\broot cause\b|\bcontribut\w*\b|"
    r"\battribute\b|\bbecause of\b|\breason for\b",
    re.I,
)
# 数据词:变化词(为什么…下降) + 业务指标/实体词(贡献/营收/利润…)。
# 「最大/最高/最多」归入数据词:「哪个地区贡献最大」= 典型归因问题。
_ATTRIBUTION_DATA = re.compile(
    r"下降|上升|上涨|下滑|增长|减少|增加|下跌|波动|变化|回升|回落|"
    r"最大|最高|最多|最低|"
    r"营收|利润|业绩|销售|金额|数量|贷款|订单|用户|客户|交易|余额|额度|增速|"
    r"\b(?:decline|declined|drop|dropped|fall|fell|rise|rose|increase|increased|"
    r"decrease|decreased|grow|growth|change|revenue|profit|sales|amount|volume|"
    r"loan|order|user|customer|transaction|balance|highest|lowest|most)\b",
    re.I,
)


def has_attribution_signal(question: str) -> bool:
    """归因/根因问题强信号:触发词 + 数据词双命中。

    双命中保证不误伤 chitchat(「为什么天是蓝的」无数据词)与普通查询
    (「哪个地区金额最高」命中最大/金额但无触发词)。归因 intent 仍走
    query 主链,只让下游 query_sketch 产出归因计划、attribution 节点
    多跳下钻——不改变取数主流程。
    """
    if not (question or "").strip():
        return False
    return bool(
        _ATTRIBUTION_TRIGGER.search(question)
        and _ATTRIBUTION_DATA.search(question)
    )


# ── 草稿确认:管理员在对话中采纳语义扩展(走 refuse 的 pending draft)──────

_CONFIRM_RE = re.compile(
    r"^(?:"
    r"确认(?:草稿|这个草稿|这个|吧|一下)?"
    r"|同意|批准|认可|采纳"
    r"|approve(?: it)?|confirm(?: the draft| it)?|yes|ok"
    r")\s*[。．.!！?？]?\s*$",
    re.I,
)


def has_strong_confirm(question: str) -> bool:
    """草稿确认强信号:整句即确认语(高精度,宁漏勿误)。

    只接受独立成句的确认(确认/同意/批准/approve…),避免把"确认贷款金额"
    "通过率"等数据问题误判为确认操作。具体权限/草稿存在性在 confirm_draft
    节点内二次校验。
    """
    return bool(_CONFIRM_RE.match((question or "").strip()))


def has_followup_signal(question: str, history: str) -> bool:
    """省略式追问信号:短问题 + 指代词(或 呢/吗 结尾)+ 有历史。"""
    q = (question or "").strip()
    if not q or not history:
        return False
    if len(q) > 20:
        return False
    return bool(_REFERENTIAL.search(q)) or (
        len(q) <= 6 and bool(_FOLLOWUP_TAIL.search(q))
    )


def last_user_question(history: str) -> str | None:
    """history 紧凑格式中最近一轮的用户问题(重跑上一问用)。"""
    for line in reversed((history or "").splitlines()):
        line = line.strip()
        if line.startswith("user:"):
            question = line[len("user:"):].strip()
            return question or None
    return None


# ── 分类与校验 ─────────────────────────────────────────────

def classify_intent(question: str) -> Intent | None:
    """Strong-signal classification; None when no strong signal fires.

    Priority: write > confirm > metadata > attribution > chitchat >
    correction (safety first).
    """
    if has_strong_write(question):
        return Intent.WRITE
    if has_strong_confirm(question):
        return Intent.CONFIRM
    for pattern in _STRONG_COMPILED:
        if pattern.search(question):
            return Intent.METADATA
    if has_attribution_signal(question):
        return Intent.ATTRIBUTION
    if has_strong_chitchat(question):
        return Intent.CHITCHAT
    if has_strong_correction(question):
        return Intent.CORRECTION
    return None


def has_weak_signal(question: str) -> bool:
    """Whether the input carries a weak metadata-ish signal (LLM confirms)."""
    return any(p.search(question) for p in _WEAK_COMPILED)


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
    strong_match: bool = False,
    write_signal: bool = False,
    confirm_signal: bool = False,
    chitchat_signal: bool = False,
    correction_signal: bool = False,
    followup_signal: bool = False,
    history_present: bool = False,
    weak_signal: bool = False,
    mentioned_table: bool = False,
    term_hit: bool = False,
    data_signal: bool = False,
    attribution_signal: bool = False,
) -> Intent:
    """Deterministic verification of the LLM's intent verdict.

    Priority: write (safety) > draft confirm > elliptical follow-up >
    pure feedback > metadata evidence > attribution signal > LLM
    write/correction trust > permissive QUERY.

    Args:
        llm_intent: The LLM's five-way classification.
        strong_match: A strong metadata regex fired on the question.
        write_signal: A mutation verb + data object is present.
        confirm_signal: A draft-confirmation phrase fired on the question
            (admin chat-confirm flow, see has_strong_confirm).
        chitchat_signal: Pure chitchat template hit, no data words.
        correction_signal: Pure-feedback phrase hit, no substance.
        followup_signal: Short elliptical question with referential
            pronoun (or 呢/吗 ending).
        history_present: The session carries prior exchanges.
        weak_signal: A weak metadata-ish signal fired on the question.
        mentioned_table: The question mentions a known table.
        term_hit: The question hits a known business term.
        data_signal: The question carries a data-question signal
            (count/list/percent/ordered patterns from workflow.rules).
        attribution_signal: The question is a "why/contribution/root-cause"
            business question (trigger + data words, see
            has_attribution_signal).

    Returns:
        The verified intent.
    """
    # 安全第一:写意图正则命中 → 无条件拒绝(即使 LLM 判 query)
    if write_signal:
        return Intent.WRITE
    # 草稿确认:高精度确认词 + 有历史上下文 → 路由到对话内确认节点
    # (节点内部再做管理员权限 + 草稿存在性二次校验)
    if confirm_signal and history_present:
        return Intent.CONFIRM
    # 省略式追问(指代词/短问尾 + 历史)→ 需补全后继续
    if followup_signal and history_present:
        return Intent.CORRECTION
    # 纯反馈纠正(无数据/元数据实质)→ 重跑上一问
    if correction_signal and not data_signal and not weak_signal and not strong_match:
        return Intent.CORRECTION
    if llm_intent == Intent.METADATA:
        return Intent.METADATA
    if strong_match and not data_signal:
        return Intent.METADATA
    # 归因:触发词 + 数据词双命中(has_attribution_signal 内嵌 data 门禁)。
    # 放 metadata 之后、write/chitchat 信任之前——metadata 优先(「为什么
    # 口径这么定义」是 metadata),归因其次(「为什么营收下降」走 query 主链)。
    if attribution_signal:
        return Intent.ATTRIBUTION
    if llm_intent == Intent.WRITE:
        return Intent.WRITE
    # 闲聊信号独立路由(LLM 不可用/误判时也生效)——数据词防护在
    # has_strong_chitchat 内,data_signal 兜底双重校验
    if chitchat_signal and not data_signal:
        return Intent.CHITCHAT
    # LLM 判闲聊(正则未命中:拼音问候、变体寒暄等)→ 无数据信号则信任。
    # 闲聊误判代价最低(一句 canned 回复),数据问题误吞代价高,故 data 信号兜底
    if llm_intent == Intent.CHITCHAT and not data_signal:
        return Intent.CHITCHAT
    if llm_intent == Intent.CORRECTION and (
        history_present or correction_signal or followup_signal
    ):
        return Intent.CORRECTION
    return Intent.QUERY
