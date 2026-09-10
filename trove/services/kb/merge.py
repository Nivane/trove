"""资产的三方合并 ``merge3(base, ours, theirs)`` —— Phase C2。

**为什么是条目粒度,不是文本行**:资产是结构化列表(表、字段、指标、示例、
经验),文本行合并没有意义 —— 一次 YAML 重排(缩进、引号、键序)就能让每一
行都"变了",而内容一个字没改。合并比的是 ``yaml.safe_load`` 之后的**规范化
结构**,格式差异自然收敛为 no-op。

**base 是什么**:上一次**完整重新生成**时生成方写下的内容。它必须是一份纯
生成物,否则"人改过这条没有"就无从判断 —— 增量写入器(``draft_example`` /
``append_term``)的输出不能当 base:那样人的编辑会混进祖先,下一轮生成就会
名正言顺地把它们覆盖掉。

**逐条规则**(``ours`` = 盘上的文件,``theirs`` = 这次生成的内容):

| base | ours | theirs | 结果 |
|---|---|---|---|
| = ours | — | ≠ base | theirs(人没动这条) |
| = theirs | ≠ base | — | ours(**编辑存活**,本设计的目的) |
| 都有 | ≠ base | ≠ base | 相等 → ours;不等 → ours + 记冲突 |
| 有 | 删 | = base | 删(尊重人的删除) |
| 有 | 删 | ≠ base | 删 + 记冲突(说不清,但删除是人的显式意图) |
| 有 | 在 | 删 | 删(尊重生成方的删除,如指标被移除) |
| 有 | 改过 | 删 | ours + 记冲突(说不清就不销毁) |
| 无 | 有 | 无 | ours(人手工加的) |
| 无 | 无 | 有 | theirs(生成方新增) |

冲突一律**保留 ours 且不阻断合并**:合并的结果永远是"能被下游读的一份完整
资产",人拿到的是一份报告而不是一次中断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from trove.services.kb.provenance import META_KEY

#: 条目缺失(与"值为 None"不同:None 是一个真实的值)。
MISSING = object()

#: 嵌套列表的身份键。顶层 section 的身份键由 :data:`FILE_SPECS` 给。
NESTED_IDENTITY = "name"


@dataclass(frozen=True)
class FileSpec:
    """一份资产的结构:条目列表在哪个顶层键下、身份键是什么。"""

    section: str
    identity: str


#: 参与合并的资产。不在表里的文件按**整份文档**合并(通用规则)。
#: ``rules.yml`` 是纯人工文件(没有任何写入器),不参与生成侧合并 ——
#: 显式列成 ``None`` 而不是"漏了",这样它永远不会被误当成生成物。
FILE_SPECS: dict[str, FileSpec | None] = {
    "schema_notes.yml": FileSpec("tables", "name"),
    "semantics.yml": FileSpec("semantic_model", "name"),
    "examples.yml": FileSpec("examples", "question"),
    "lessons.yml": FileSpec("lessons", "pattern"),
    "rules.yml": None,
}

#: 冲突种类(报告里原样出现,别改字面量)。
BOTH_MODIFIED = "both_modified"
ADDED_VS_ADDED = "added_vs_added"
DELETED_VS_MODIFIED = "deleted_vs_modified"


@dataclass(frozen=True)
class Conflict:
    """一条说不清的差异:双方都动过,合并没有正确答案。"""

    path: str
    kind: str
    ours: Any
    theirs: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "ours": self.ours,
            "theirs": self.theirs,
        }


@dataclass(frozen=True)
class MergeResult:
    doc: dict[str, Any]
    conflicts: tuple[Conflict, ...]
    added: int = 0
    updated: int = 0
    kept: int = 0
    removed: int = 0

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "kept": self.kept,
            "removed": self.removed,
            "conflicts": [c.as_dict() for c in self.conflicts],
        }


@dataclass
class _Ctx:
    conflicts: list[Conflict] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    kept: int = 0
    removed: int = 0

    def conflict(self, path: str, ours: Any, theirs: Any, kind: str) -> None:
        # 在**构造时**把 MISSING 归一成 None:报告要过 API 序列化,漏一处
        # `json.dumps` 就会撞上哨兵对象而 500。
        self.conflicts.append(Conflict(
            path=path, kind=kind, ours=_display(ours), theirs=_display(theirs),
        ))


def _display(value: Any) -> Any:
    """报告里的值:缺失显式写成 ``None``,别让读者以为是空值。"""
    return None if value is MISSING else value


def _eq(a: Any, b: Any) -> bool:
    """结构相等。``MISSING`` 只与 ``MISSING`` 相等。"""
    if a is MISSING or b is MISSING:
        return a is b
    return a == b


def _body(doc: Any) -> dict[str, Any]:
    """去掉 ``_meta`` 的正文。

    ``_meta`` 每次写盘都新(时间戳),留着它会让每一份文件在合并时都先炸出
    一条 ``_meta`` 冲突。它也不该参与合并:合并结果由调用方重新盖章。
    """
    if not isinstance(doc, Mapping):
        return {}
    return {k: v for k, v in doc.items() if k != META_KEY}


def _as_entry_map(value: Any, identity: str | None) -> dict[str, Any] | None:
    """``list[dict]`` → ``{身份键: 条目}``;定不了键 → ``None``。

    ``identity=None`` 时键就是映射自身的键(文档层与映射层)。

    定不了键就整块按值处理,这是**保守**的一侧:例如 ``expression.dialects``
    是 ``[{dialect, expression}]``,没有身份键 —— 按条目合并没有意义(哪个
    dialect 算"同一条"?),整块替换才是对语义。
    """
    if identity is None:
        return dict(value) if isinstance(value, Mapping) else None
    if not isinstance(value, list):
        return None
    out: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        key = item.get(identity)
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            return None
        if key in out:
            return None  # 身份键重复 → 定不了序,整块处理更安全
        out[key] = item
    return out


def _keyed(value: Any, identity: str | None) -> bool:
    return _as_entry_map(value, identity) is not None


def _merge_keyed(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    path: str,
    ctx: _Ctx,
    identity: str | None,
) -> dict[str, Any]:
    """逐键合并。键的顺序:``ours`` 的顺序,再补 ``theirs`` 的新键。

    顺序按 ours 是刻意的:人重排过一次列表,不该被生成方改回去。
    """
    out: dict[str, Any] = {}
    keys = [*ours, *(k for k in theirs if k not in ours)]
    for key in keys:
        kp = f"{path}.{key}" if identity is None else f"{path}[{key}]"
        b, o, t = (
            base.get(key, MISSING),
            ours.get(key, MISSING),
            theirs.get(key, MISSING),
        )
        if o is MISSING:
            if b is MISSING:
                if t is not MISSING:
                    out[key] = t
                    ctx.added += 1
                continue
            # 有 base、ours 删了
            if t is MISSING or _eq(t, b):
                ctx.removed += 1  # 人删的,生成方没动(或也删了)→ 删
                continue
            ctx.removed += 1
            ctx.conflict(kp, MISSING, t, DELETED_VS_MODIFIED)
            continue
        if b is MISSING:
            if t is MISSING:
                out[key] = o
                ctx.kept += 1  # 人手工加的
                continue
            if _eq(o, t):
                out[key] = o
                continue
            out[key] = o
            ctx.conflict(kp, o, t, ADDED_VS_ADDED)  # 祖先不明,不猜
            continue
        # base / ours / theirs 三方都在场
        if _eq(b, o):  # 人没动这条
            if t is MISSING:
                ctx.removed += 1  # 生成方删了(如指标被移除)→ 删
                continue
            out[key] = t
            if not _eq(o, t):
                ctx.updated += 1
            continue
        if t is MISSING:
            out[key] = o
            ctx.kept += 1
            ctx.conflict(kp, o, MISSING, DELETED_VS_MODIFIED)  # 不销毁人的修改
            continue
        if _eq(t, b):
            out[key] = o
            ctx.kept += 1  # 生成方没动 → 编辑存活
            continue
        if _eq(o, t):
            out[key] = o
            continue
        merged = _merge_node(b, o, t, kp, ctx)
        if merged is MISSING:
            out[key] = o
            ctx.kept += 1
            ctx.conflict(kp, o, t, BOTH_MODIFIED)
        else:
            out[key] = merged
    return out


def _merge_list(
    base: Any, ours: Any, theirs: Any, path: str, ctx: _Ctx, identity: str,
) -> list[Any]:
    o_map = _as_entry_map(ours, identity) or {}
    t_map = _as_entry_map(theirs, identity) or {}
    b_map = _as_entry_map(base, identity) or {}
    merged = _merge_keyed(b_map, o_map, t_map, path, ctx, identity)
    return list(merged.values())


def _merge_node(
    base: Any, ours: Any, theirs: Any, path: str, ctx: _Ctx,
) -> Any:
    """三方都在场的**可比**节点:能下沉就下沉,不能则返回 ``MISSING``。"""
    if all(_keyed(v, NESTED_IDENTITY) for v in (base, ours, theirs)):
        return _merge_list(base, ours, theirs, path, ctx, NESTED_IDENTITY)
    if all(isinstance(v, Mapping) for v in (base, ours, theirs)):
        return _merge_keyed(
            _body(base), _body(ours), _body(theirs), path, ctx, None,
        )
    return MISSING


def merge3(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    filename: str,
) -> MergeResult:
    """三方合并一份资产。纯函数:不读盘、不写盘、不改入参。

    ``base`` 是上一次生成方的输出(见模块文档),``ours`` 是盘上的文件,
    ``theirs`` 是这次生成的内容。
    """
    spec = FILE_SPECS.get(filename)
    if spec is None:
        if filename in FILE_SPECS:  # rules.yml:纯人工文件,原样保留
            return MergeResult(doc=_body(ours), conflicts=())
        # 不在表里的文件(如人自己放进来的 YAML):整份文档按通用规则合并
        ctx = _Ctx()
        merged = _merge_node(_body(base), _body(ours), _body(theirs), filename, ctx)
        doc = _body(ours) if merged is MISSING else merged
        return MergeResult(doc=doc, conflicts=tuple(ctx.conflicts), **_counts(ctx))

    ctx = _Ctx()
    section = spec.section
    b_sec, o_sec, t_sec = (
        base.get(section, MISSING), ours.get(section, MISSING), theirs.get(section, MISSING),
    )
    if _keyed(o_sec, spec.identity) and _keyed(t_sec, spec.identity):
        merged_section: Any = _merge_list(
            b_sec if b_sec is not MISSING else [],
            o_sec, t_sec, section, ctx, spec.identity,
        )
    else:
        # 条目列表本身无法定键(结构被人改坏了)→ 退回整块规则
        merged_section = o_sec if o_sec is not MISSING else t_sec

    # 除 section 以外的顶层键(如 OSSIE ``version``)照常合并
    rest = _merge_keyed(
        {k: v for k, v in _body(base).items() if k != section},
        {k: v for k, v in _body(ours).items() if k != section},
        {k: v for k, v in _body(theirs).items() if k != section},
        "", ctx, None,
    )
    doc = dict(rest)
    if merged_section is not MISSING:
        doc[section] = merged_section
    return MergeResult(doc=doc, conflicts=tuple(ctx.conflicts), **_counts(ctx))


def entry_count(doc: Mapping[str, Any], filename: str) -> int:
    """一份资产里有多少条**可合并的条目**(报告用:说清风险有多大)。

    数不出来(结构被人改坏、或不在白名单里)→ 数顶层列表的长度,再不行给 0。
    """
    spec = FILE_SPECS.get(filename)
    body = _body(doc)
    if spec is not None:
        section = body.get(spec.section)
        if isinstance(section, list):
            return len(section)
        return 0
    return sum(len(v) for v in body.values() if isinstance(v, list))


def _counts(ctx: _Ctx) -> dict[str, int]:
    return {
        "added": ctx.added,
        "updated": ctx.updated,
        "kept": ctx.kept,
        "removed": ctx.removed,
    }
