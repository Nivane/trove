"""漂移治理的领域模型 —— 四级分类学 + 合流后的统一证据形态。

**为什么要有这一层**:Trove 已经有两套漂移检测,它们各自返回自己的形状:

- ``memory/schema_drift.detect_drift`` —— **L1 结构层**,KB ``schema_notes.yml``
  vs 活库 column_sets。
- ``SemanticLayerProvider._compute_drift`` —— **L2 引用层**,``semantics.yml``
  声明的表/字段/键/关系端点 vs catalog 快照。

两者都是对的,但**输出无法相加**:一边是 ``column_changes[table].added``,另
一边是 ``missing_fields[dataset]``;下游(API / CLI / 门禁 / 影响面)要为每一种
形状写一遍。本模块定义唯一证据形态 :class:`DriftItem`,原报告全量降入
``detail`` —— 归一不是丢弃。

**四级分类学**(判据:「语义模型对物理世界做过的承诺是否仍成立」):

===========  ==========  ==========================  ==========
级别          名称        承诺类型                     可自动检测
===========  ==========  ==========================  ==========
``L1``        结构漂移    对象存在                     ✅
``L2``        引用漂移    引用可达                     ✅
``L3``        语义漂移    取值可锚定 / 关系可加          ✅(需活库)
``L4``        口径漂移    —(上游逻辑变更)              ❌ 仅承接
===========  ==========  ==========================  ==========

L4 明确**不检测**、只承接外部声明,是为了把边界写在设计里而不是留给实现者猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 级别与状态(用常量而非 Enum:要直接落库、进 JSON,且与 YAML 字面一致)──

L1 = "L1"  # 结构漂移:对象存在
L2 = "L2"  # 引用漂移:引用可达
L3 = "L3"  # 语义漂移:取值可锚定 / 关系可加
L4 = "L4"  # 口径漂移:仅承接外部声明

LEVELS = (L1, L2, L3, L4)

STATUS_OPEN = "open"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_RESOLVED = "resolved"
STATUS_WAIVED = "waived"

#: 条目级状态。``skipped`` 不在此列 —— 它是 **run 级** 的(见 DriftReport),
#: 「这次没检测」与「这条已处理」是两件事,混用会让 I3 失效。
ITEM_STATUSES = (STATUS_OPEN, STATUS_ACKNOWLEDGED, STATUS_RESOLVED, STATUS_WAIVED)

RUN_OK = "ok"
RUN_SKIPPED = "skipped"  # 未检测(catalog 不可达 / KB 缺失)
RUN_ERROR = "error"

SKIP_CATALOG_UNREACHABLE = "catalog_unreachable"
SKIP_KB_MISSING = "kb_missing"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}

SOURCE_DETECTOR = "detector"
SOURCE_EXTERNAL = "external"

#: 会被门禁阻断的严重度。``info`` 永不阻断 —— 「活库多了张表」不该拦住
#: 一次语义变更。
BLOCKING_SEVERITIES = (SEVERITY_WARNING, SEVERITY_CRITICAL)


def severity_rank(sev: str) -> int:
    return SEVERITY_ORDER.get(sev, 0)


@dataclass(frozen=True)
class DriftItem:
    """一条漂移证据(合流后的唯一形态)。

    ``subject`` 是**规范化主体标识**,同时用于两处:落库的唯一键的一部分,
    以及门禁 coverage 判定(与变更的 ``touched_subjects`` 求交集)。因此它
    必须稳定:同一处漂移在多次检测中生成同一个 subject。

    形态约定:

    - 表级 → ``"<table>"``
    - 列/字段级 → ``"<table>.<column>"``(小写、去 schema 前缀)
    - 关系级 → ``"<relationship_name>"``

    ``detail`` 保留**原始检测器的原字段**。归一不等于丢弃:排查时需要的
    往往是被归一掉的上下文(表达式原文、探测到的实际值)。
    """

    level: str
    kind: str
    subject: str
    severity: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown drift level: {self.level!r}")
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if not self.subject:
            raise ValueError("DriftItem.subject must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "kind": self.kind, "subject": self.subject,
                "severity": self.severity, "detail": dict(self.detail)}


@dataclass(frozen=True)
class ImpactSet:
    """一处漂移的爆炸半径(发现时的判断快照)。

    存快照而非实时计算:影响面随 KB 演进而变化,事后复盘需要的是**当初为
    什么这么判**,不是现在会怎么判。
    """

    metrics: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.metrics or self.examples or self.rules or self.lessons)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "metrics": list(self.metrics),
            "examples": list(self.examples),
            "rules": list(self.rules),
            "lessons": list(self.lessons),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ImpactSet":
        d = d or {}
        return cls(
            metrics=list(d.get("metrics") or []),
            examples=list(d.get("examples") or []),
            rules=list(d.get("rules") or []),
            lessons=list(d.get("lessons") or []),
        )


@dataclass(frozen=True)
class DriftReport:
    """一次检测的结果。

    ``status`` 是**必填**的,且 ``skipped`` 与空 ``items`` 是两件事 ——
    这正是 I3:「空报告 ≠ 无漂移」。两个现有检测器在 catalog 不可达时都返回
    空报告(``schema_drift.py:46`` / ``provider._compute_drift`` 的 ``model
    is None`` 分支),调用方若不区分,就会把「没查成」读成「没问题」。

    ``levels_verified`` 是 ``status`` 之外**第二道** I3 防线。``status`` 只说
    「这次检测整体完成了吗」,说不了「哪几级真的查了」。一次只请求 L1 的检测
    是 ``ok`` 的,但它对 L2 一无所知;一份 datasource 没有语义层的 ``ok``
    报告,对 L2 同样一无所知。门禁若只看 ``ok``,这两种情况都会被读成
    「L2 干净」。

    所以名字是 **verified** 而不是 checked:它列的是「本次**验证通过**的
    级别」。``skipped`` 的检测恒为空集 —— 未完成的检测什么都没验证,
    哪怕其中一路跑完了也一样(那半份结论没有兜底,不能单独采信)。
    """

    datasource: str
    status: str
    items: list[DriftItem]
    generated_at: str
    skip_reason: str | None = None
    new_count: int = 0
    error: str | None = None
    levels_verified: frozenset[str] = frozenset()

    def verified(self, level: str) -> bool:
        """该级别是否**在本次检测中被验证通过**。门禁的 coverage 判定入口。"""
        return self.status == RUN_OK and level in self.levels_verified

    @property
    def ok(self) -> bool:
        return self.status == RUN_OK

    def by_level(self, level: str) -> list[DriftItem]:
        return [i for i in self.items if i.level == level]

    def blocking(self) -> list[DriftItem]:
        return [i for i in self.items if i.severity in BLOCKING_SEVERITIES]

    def to_dict(self) -> dict[str, Any]:
        """对外形状 —— API 与 CLI **共用这一份**。

        两个前门各写一遍 ``to_dict`` 是漂移的温床:``levels_verified`` 这种
        后加的字段会只出现在其中一边,而消费方看不出少了的那个键意味着什么。
        """
        return {
            "datasource": self.datasource,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "generated_at": self.generated_at,
            "detected": len(self.items),
            "new_count": self.new_count,
            # 门禁的输入:哪几级**验证通过**了。只有 status=ok 不够 ——
            # 一次只查了 L1 的 ok 报告,对 L2 一无所知。
            "levels_verified": sorted(self.levels_verified),
            "items": [i.to_dict() for i in self.items],
        }


@dataclass(frozen=True)
class DriftRow:
    """落库后的漂移条目(``DriftItem`` + 生命周期字段)。"""

    id: int
    datasource: str
    level: str
    kind: str
    subject: str
    detail: dict[str, Any]
    affected: dict[str, list[str]]
    severity: str
    status: str
    source: str
    first_seen_at: str
    last_seen_at: str
    seen_count: int
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolve_reason: str | None = None


@dataclass(frozen=True)
class SemanticChange:
    """一次语义变更动了什么 —— 门禁 coverage 判定的输入。

    ``touched_subjects`` 用与 :class:`DriftItem.subject` **同一套规范化**,
    否则交集永远为空、门禁形同虚设。
    """

    datasource: str
    kind: str  # draft_confirm | manual_edit | kb_init | external
    touched_subjects: frozenset[str] = frozenset()
    author: str = ""


@dataclass(frozen=True)
class GateDecision:
    """门禁判定。``action`` 三态:allow / warn / block。"""

    action: str
    blocking: list[DriftItem] = field(default_factory=list)
    covered: list[DriftItem] = field(default_factory=list)
    message: str = ""

    @property
    def allowed(self) -> bool:
        return self.action != "block"


ALLOW = "allow"
WARN = "warn"
BLOCK = "block"


def normalize_subject(raw: str) -> str:
    """主体名规范化 —— 落库键与 coverage 判定**必须**共用这一份。

    规则:小写、去引号/反引号/方括号(逐段)、压空白;三段及以上取末两段 ——
    ``public.orders.id`` 与 ``orders.id`` 归一到同一主体。物理 schema 前缀在
    语义层是可选的(见 ``rls.physical_table``),不归一会让同一处漂移在两条
    检测路径下产生两个 subject。

    **两段输入是歧义的,且这里的取舍是「保留两段」**:``public.orders`` 既可
    读作 ``schema.table`` 也可读作 ``table.column``,没有 schema 清单就无法从
    字面上判定。两个方向的代价完全不对称 —— 保留只是让一对本应合并的主体各
    占一行(可见、可查);剥离则会把 ``orders.id`` 与 ``users.id`` 一起塌成
    ``id``,不同表的同名列合并成一条记录,``seen_count`` 与影响面从此都在说谎。
    所以宁可少合并,不可错合并。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    s = " ".join(s.split()).lower()
    # 引号必须**逐段**剥:``"orders"."id"`` 只在整串首尾剥会剩 ``orders"."id``。
    parts = [p.strip().strip('`"[]').strip() for p in s.split(".")]
    parts = [p for p in parts if p]
    # 三段以上才剥 schema:保留最后两段(table.column)或一段(table)。
    # 恰好两段时**不剥** —— 见上面「少合并,不错合并」。
    if len(parts) > 2:
        parts = parts[-2:]
    return ".".join(parts)
