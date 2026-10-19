"""验收集(evalset)的资产模型 —— 闭集枚举、dataclass、容错解析与状态机。

``.trove/evalsets/<datasource>/evalset.yml`` 是资产的唯一真相源:

  - ``source`` 只描述**题目从哪来**(production / semantic / synthetic /
    manual / benchmark),``reference_origin`` 只描述**参考 SQL 怎么来的**
    (human / pipeline / semantic_compiler / llm);二者不混。
  - ``tier`` 是**读时派生**(:func:`tier_of`),不落盘,与 ``status`` 无关
    —— pending 条目也有 prospective tier,供待审队列/覆盖视图分组;只有
    ``status=confirmed`` 的条目参与评分。
  - 枚举是闭集:解析时未知值**报 issue 但保留条目**(容错读),绝不静默
    折叠成默认值 —— 一个悄悄走偏的 source 最后会变成一条被算进错误 tier
    的评分题。

写方向只有一条纪律:``dump_evalset`` 必须让 ``parse_evalset`` 读回同样的
文档(round-trip 无损),未知键一律进 ``extra`` 原样带回去。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

from trove.services.kb.ledger import normalize_question

#: ``evalset.yml`` 的**格式**版本(不是修订计数 —— 修订历史由 git 承担)。
EVALSET_VERSION = 1

#: 题目来源闭集。``benchmark`` 为外部基准对照预留(P0 不生成)。
SOURCES = ("production", "semantic", "synthetic", "manual", "benchmark")

#: 参考 SQL 来源闭集。
ORIGINS = ("human", "pipeline", "semantic_compiler", "llm")

#: 条目状态闭集(墓碑 ``rejected`` 参与生成去重;``retired`` 不参与)。
STATUSES = ("pending", "confirmed", "rejected", "retired")

#: 结果比对方式闭集。``shape`` 是 schema 预留值(P0 不可确认/不可运行)。
MATCHES = ("exact", "shape")

#: 评分分层的展示/遍历顺序(``unknown`` 殿后)。分数永不跨 tier 合并。
TIER_ORDER = ("verified", "semantic", "synthetic", "benchmark", "unknown")

#: 生成请求的默认条数。
DEFAULT_GENERATE_LIMIT = 30

#: 单次生成请求的硬上限(组合池再大也不越过)。
MAX_GENERATE_PER_RUN = 100

#: 批量确认/批量操作的条数上限(与行动层批量同源)。
MAX_BATCH = 200

#: 时间粒度的声明顺序(覆盖视图与模板枚举共用)。
GRAIN_ORDER = ("month", "week", "day", "quarter", "year")

#: 使 ``duplicate_of`` 标记自动清除的终态(设计 §5.2)。
_TERMINAL_STATUSES = frozenset({"confirmed", "rejected", "retired"})

#: 状态机:动作 → 允许的**当前**状态。``confirmed`` 的编辑另需 note(服务层)。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "confirm": frozenset({"pending"}),
    "reject": frozenset({"pending"}),
    "edit": frozenset({"pending", "confirmed"}),
    "vouch": frozenset({"confirmed"}),
    "retire": frozenset({"confirmed"}),
}

#: 未知枚举值的 issue code。
_UNKNOWN_CODES = {
    "source": "evalset.unknown_source",
    "reference_origin": "evalset.unknown_origin",
    "status": "evalset.unknown_status",
    "match": "evalset.unknown_match",
}

#: ``EvalsetItem`` 的已知键(解析时其余键进 ``extra``,dump 时追加回去)。
_ITEM_KEYS = frozenset({
    "qid", "question", "reference_sql", "source", "reference_origin", "status",
    "match", "tags", "content_hash", "needs_reference", "duplicate_of",
    "leakage", "leakage_hit", "note", "created_at", "created_by", "generator",
    "confirmed_at", "confirmed_by", "vouched_at", "vouched_by",
    "rejection_reason", "retired_at",
})

#: 文档顶层已知键(其余键进 ``doc.extra``)。
_DOC_KEYS = frozenset({"version", "datasource", "updated_at", "items"})


@dataclass
class EvalsetIssue:
    """一条解析问题(不抛异常:读路径容错,写/运行路径按 issues 拒绝启动)。"""

    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "code": self.code, "message": self.message}


class EvalsetError(Exception):
    """一次**必须拒绝**的 evalset 操作(解析 issue 则走 :class:`EvalsetIssue`)。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        issues: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = list(issues or [])


@dataclass
class EvalsetItem:
    """一条验收集条目(``evalset.yml`` 的 ``items[]``)。

    ``tier`` 不存在字段里 —— 它由 :func:`tier_of` 读时派生;落盘会让
    「来源变了但 tier 没刷新」成为一种可持久化的不一致。
    """

    qid: str = ""
    question: str = ""
    reference_sql: str = ""
    source: str = ""
    reference_origin: str = ""
    status: str = "pending"
    match: str = "exact"
    tags: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    needs_reference: bool = False
    duplicate_of: str = ""
    leakage: bool = False
    leakage_hit: str = ""
    note: str = ""
    created_at: str = ""
    created_by: str = ""
    generator: str = ""
    confirmed_at: str = ""
    confirmed_by: str = ""
    vouched_at: str = ""
    vouched_by: str = ""
    rejection_reason: str = ""
    retired_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """已知键全写出(含空串/False/[]),``extra`` 追加在后。"""
        out: dict[str, Any] = {
            "qid": self.qid,
            "question": self.question,
            "reference_sql": self.reference_sql,
            "source": self.source,
            "reference_origin": self.reference_origin,
            "status": self.status,
            "match": self.match,
            "tags": dict(self.tags),
            "content_hash": self.content_hash,
            "needs_reference": bool(self.needs_reference),
            "duplicate_of": self.duplicate_of,
            "leakage": bool(self.leakage),
            "leakage_hit": self.leakage_hit,
            "note": self.note,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "generator": self.generator,
            "confirmed_at": self.confirmed_at,
            "confirmed_by": self.confirmed_by,
            "vouched_at": self.vouched_at,
            "vouched_by": self.vouched_by,
            "rejection_reason": self.rejection_reason,
            "retired_at": self.retired_at,
        }
        for key, value in self.extra.items():
            out.setdefault(key, value)
        return out


@dataclass
class EvalsetDoc:
    """一份验收集文档(一个数据源恰好一份)。"""

    datasource: str = ""
    version: int = EVALSET_VERSION
    updated_at: str = ""
    items: list[EvalsetItem] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "datasource": self.datasource,
            "updated_at": self.updated_at,
            "items": [it.to_dict() for it in self.items],
        }
        for key, value in self.extra.items():
            out.setdefault(key, value)
        return out


def now_iso() -> str:
    """UTC 秒级 ISO 8601 时间戳(``2026-10-19T10:00:00+00:00``)。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_qid() -> str:
    """新条目 id:``e-`` + uuid4 十六进制前 10 位。创建时分配,永不变。"""
    return "e-" + uuid.uuid4().hex[:10]


def normalize_sql(sql: str) -> str:
    """SQL 归一化:小写 + 折叠空白(与内容哈希同尺子)。"""
    return re.sub(r"\s+", " ", (sql or "").strip().lower())


def content_hash(question: str, reference_sql: str) -> str:
    """内容哈希 = ``sha256(ledger 归一问题 + "\\n" + 归一 SQL)[:16]``。

    问题侧用 ``kb.ledger.normalize_question`` —— **与泄漏检查同一把尺子**
    (保留 ``_``);评测对账用的是另一把(``trove/eval/text.norm_question``,
    折 ``_``),两把尺子刻意不串用。SQL 为空时只哈希问题、不加换行。
    """
    normalized_question = normalize_question(question)
    normalized_sql = normalize_sql(reference_sql)
    payload = (
        normalized_question
        if not normalized_sql
        else normalized_question + "\n" + normalized_sql
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def tier_of(item: EvalsetItem) -> str:
    """读时派生评分分层(与 status 无关)。

    顺序即优先级:``benchmark`` 是外部基准(即使 origin=human 也不进业务
    准确率);``source ∈ {production, manual}`` 或人工参考 SQL = ``verified``;
    语义编译器产出的参考 SQL = ``semantic``(测保真度);LLM 产出 =
    ``synthetic``;其余(来源/来源说明缺失或未知)= ``unknown``。
    """
    if item.source == "benchmark":
        return "benchmark"
    if item.source in {"production", "manual"} or item.reference_origin == "human":
        return "verified"
    if item.reference_origin == "semantic_compiler":
        return "semantic"
    if item.reference_origin == "llm":
        return "synthetic"
    return "unknown"


def _text(value: Any) -> str:
    """容忍 YAML 空值:``None`` → ``""``,其余 ``str``。"""
    return "" if value is None else str(value)


def _as_bool(value: Any) -> bool:
    """YAML 布尔容忍:显式字符串 ``"false"`` 不能被 ``bool("false")`` 翻成真。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on"}
    return bool(value)


def _enum(
    raw: Mapping[str, Any],
    key: str,
    path: str,
    allowed: tuple[str, ...],
    default: str,
    issues: list[EvalsetIssue],
) -> str:
    """读一个闭集字段:缺失/空 → default;未知值 → issue 但保留原值。"""
    value = raw.get(key)
    text = "" if value is None else str(value).strip()
    if not text:
        return default
    if text not in allowed:
        issues.append(EvalsetIssue(
            f"{path}.{key}",
            _UNKNOWN_CODES[key],
            f"unknown {key} {text!r}; expected one of {', '.join(allowed)}",
        ))
    return text


def _parse_item(
    raw: Mapping[str, Any], path: str, issues: list[EvalsetIssue],
) -> EvalsetItem:
    return EvalsetItem(
        qid=_text(raw.get("qid")).strip(),
        question=_text(raw.get("question")),
        reference_sql=_text(raw.get("reference_sql")),
        source=_enum(raw, "source", path, SOURCES, "", issues),
        reference_origin=_enum(raw, "reference_origin", path, ORIGINS, "", issues),
        status=_enum(raw, "status", path, STATUSES, "pending", issues),
        match=_enum(raw, "match", path, MATCHES, "exact", issues),
        tags=dict(raw["tags"]) if isinstance(raw.get("tags"), Mapping) else {},
        content_hash=_text(raw.get("content_hash")),
        needs_reference=_as_bool(raw.get("needs_reference")),
        duplicate_of=_text(raw.get("duplicate_of")).strip(),
        leakage=_as_bool(raw.get("leakage")),
        leakage_hit=_text(raw.get("leakage_hit")),
        note=_text(raw.get("note")),
        created_at=_text(raw.get("created_at")),
        created_by=_text(raw.get("created_by")),
        generator=_text(raw.get("generator")),
        confirmed_at=_text(raw.get("confirmed_at")),
        confirmed_by=_text(raw.get("confirmed_by")),
        vouched_at=_text(raw.get("vouched_at")),
        vouched_by=_text(raw.get("vouched_by")),
        rejection_reason=_text(raw.get("rejection_reason")),
        retired_at=_text(raw.get("retired_at")),
        extra={k: v for k, v in raw.items() if k not in _ITEM_KEYS},
    )


def parse_evalset(
    data: dict | None, *, datasource: str = "",
) -> tuple[EvalsetDoc, list[EvalsetIssue]]:
    """容错解析 ``evalset.yml``:问题收集为 issues,**不抛异常**。

    ``None`` = 没有资产文件(空文档);根非映射 → ``invalid_root``;``items``
    非列表 → ``invalid_items``;单项非映射 → ``invalid_item``;缺 qid →
    ``missing_qid``;重复 qid → ``duplicate_qid`` 且**只保留第一条**;未知
    枚举值 → ``unknown_*`` 且条目保留。顶层/条目未知键分别进 ``extra``。
    """
    if data is None:
        return EvalsetDoc(datasource=datasource), []
    if not isinstance(data, Mapping):
        return EvalsetDoc(datasource=datasource), [EvalsetIssue(
            "", "evalset.invalid_root",
            f"evalset root must be a mapping, got {type(data).__name__}",
        )]

    issues: list[EvalsetIssue] = []
    version = EVALSET_VERSION
    if data.get("version") is not None:
        try:
            version = int(data["version"])
        except (TypeError, ValueError):
            version = EVALSET_VERSION

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        issues.append(EvalsetIssue(
            "items", "evalset.invalid_items",
            f"items must be a list, got {type(raw_items).__name__}",
        ))
        raw_items = []

    items: list[EvalsetItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items):
        path = f"items[{index}]"
        if not isinstance(raw, Mapping):
            issues.append(EvalsetIssue(
                path, "evalset.invalid_item",
                f"item must be a mapping, got {type(raw).__name__}",
            ))
            continue
        qid = _text(raw.get("qid")).strip()
        if not qid:
            issues.append(EvalsetIssue(
                f"{path}.qid", "evalset.missing_qid", "item has no qid",
            ))
            continue
        if qid in seen:
            issues.append(EvalsetIssue(
                f"{path}.qid", "evalset.duplicate_qid",
                f"duplicate qid {qid!r}; keeping the first occurrence",
            ))
            continue
        seen.add(qid)
        items.append(_parse_item(raw, path, issues))

    doc = EvalsetDoc(
        datasource=_text(data.get("datasource")).strip() or datasource,
        version=version,
        updated_at=_text(data.get("updated_at")),
        items=items,
        extra={k: v for k, v in data.items() if k not in _DOC_KEYS},
    )
    return doc, issues


def dump_evalset(doc: EvalsetDoc) -> str:
    """序列化文档(与 :func:`parse_evalset` round-trip 无损)。"""
    return yaml.safe_dump(
        doc.to_dict(), allow_unicode=True, sort_keys=False, width=4096,
    )


def check_transition(item: EvalsetItem, action: str) -> None:
    """校验状态机动作;非法即抛 ``evalset.invalid_transition``。

    只判**状态**。``confirmed`` 编辑另需 note、vouch 另需参考 SQL 来源,
    那些是服务层的前置条件(需要 actor/note 上下文),不塞进纯状态机。
    """
    allowed = _TRANSITIONS.get(action)
    if allowed is None:
        raise EvalsetError(
            "evalset.invalid_transition", f"unknown action {action!r}",
        )
    if item.status not in allowed:
        raise EvalsetError(
            "evalset.invalid_transition",
            f"cannot {action} an eval item in status {item.status!r} "
            f"(allowed from: {', '.join(sorted(allowed))})",
        )


def resolve_duplicates(doc: EvalsetDoc, qid: str) -> list[str]:
    """被引用条目进入终态时,清空指向它的 ``duplicate_of`` 标记。

    返回被清除的 qid 列表(空列表 = 没生效:目标不存在、还没进终态、或
    没有条目指向它)。清空不是删除历史:被清条目的 ``note`` 追加
    ``duplicate resolved: <qid> <status>``,管理员仍看得见这次冲突的结局。
    """
    target = next((it for it in doc.items if it.qid == qid), None)
    if target is None or target.status not in _TERMINAL_STATUSES:
        return []

    cleared: list[str] = []
    suffix = f"duplicate resolved: {qid} {target.status}"
    for it in doc.items:
        if it is target or it.duplicate_of != qid:
            continue
        it.duplicate_of = ""
        it.note = f"{it.note}; {suffix}" if it.note else suffix
        cleared.append(it.qid)
    return cleared


__all__ = [
    "DEFAULT_GENERATE_LIMIT",
    "EVALSET_VERSION",
    "EvalsetDoc",
    "EvalsetError",
    "EvalsetIssue",
    "EvalsetItem",
    "GRAIN_ORDER",
    "MATCHES",
    "MAX_BATCH",
    "MAX_GENERATE_PER_RUN",
    "ORIGINS",
    "SOURCES",
    "STATUSES",
    "TIER_ORDER",
    "check_transition",
    "content_hash",
    "dump_evalset",
    "new_qid",
    "normalize_sql",
    "now_iso",
    "parse_evalset",
    "resolve_duplicates",
    "tier_of",
]
