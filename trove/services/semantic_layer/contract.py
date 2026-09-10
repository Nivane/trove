"""编译器 → 生成通道的权威交接对象(Phase A)。

**为什么需要它。** 在 A0 之前,"这道题怎么答"在管线里有四个表示:LLM 的松
dict、`PlanQuery`(只作编译输入、编译完即丢)、渲染给 gen_sql 的散文、以及
编译器的 `compiled_sql` 字符串。没有任何东西保证四者一致 —— 而校验走的是最后
一个:把 `compiled_sql` 用 AST **反推**回 join 边/过滤/分组,再和 LLM 生成的
SQL 比。于是结构经历「→ 字符串 →(LLM)→ 字符串 → 反推」两次无谓的序列化。

`PlanContract` 把编译器**已经知道**的结构原样带下去:一次解析,交接,校验读它。
散文(`render_contract`)降级为这个对象的纯渲染 —— 信息只减不增,有 golden
断言锁住。

**A1:结构在编译期抽取一次,校验不再反推。** 全量编译的照抄校验比的是结果
形状签名(:class:`PlanSignature`)—— 投影/表集/过滤/连接数/分组宽度。这份签名
同样在这里抽好(编译器拼出的 SQL 必然可解析),校验侧只解析**生成**的那条
SQL(它是 LLM 输出,只能拿到字符串)。签名抽取失败时 ``signature`` 为 ``None``,
含义是"编译器自己的 SQL 解析不了" —— 那不该悄悄放行,由编译期日志 + 校验侧
的显式记录共同暴露(见 compiler._build_contract / compiled_sql_matches)。

**为什么字段是有序元组、且要经 wire 转换。** 契约要跨节点边界,而 LangGraph
在**每个超级步**把 state 交给 checkpointer 序列化。实测(langgraph 的
``JsonPlusSerializer``,与本仓库实际用的 saver 同一份实现)::

    frozenset({'a'})                      -> frozenset({'a'})   OK
    frozenset({('a', 'x')})               -> None               ← 静默清空
    frozenset({frozenset({...元组...})})  -> frozenset({None})  ← 部分清空
    dataclass(edges=frozenset({元组}))    -> dataclass(edges=None)
    {'k': ('a', 'b')}                     -> {'k': ['a', 'b']}  tuple 降级
    自定义类型(非注册)                  -> 严格模式下被拦

即:**"集合套元组"这一形状跨不过节点边界,会被静默清空成 ``None``** —— 而
``_skeleton_*`` 抽取器的返回类型恰好就是这个形状。所以契约在内存里用**有序
元组**表示(集合语义不变,只是把迭代顺序固定下来,顺带让渲染确定性可复现),
跨节点时经 :func:`contract_to_wire` 降为纯 JSON 形状(str/int/list/dict),
:func:`contract_from_wire` 原样还原。两者有经真实 serde 的往返测试。

``from_wire`` 遇到任何形状异常一律返回 ``None``(而不是尽量解出一部分):
部分解出的契约意味着**被削弱的校验**,那是 fail-open;``None`` 是明确的
"没有契约"信号,调用方按老路径走。区分这两者是本模块的刻意设计。

**A0 的范围**:只换接口形状,不改编译算法、不改校验判定。结构在编译期抽取
一次(当时 SQL 必然可解析),而不是在执行前重解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Col",
    "JoinEdge",
    "WhereCond",
    "PlanSignature",
    "PlanContract",
    "render_contract",
    "contract_to_wire",
    "contract_from_wire",
]

# (表, 列),全小写。表名可为空串(生成 SQL 里的未限定列)。
Col = tuple[str, str]
# 一条 ON 等值边:两端按 (表, 列) 升序规范化,方向无关。
JoinEdge = tuple[Col, Col]
# 过滤条件:(涉及列集[升序], 操作符, 字面量值元组[升序])。
WhereCond = tuple[tuple[Col, ...], str, tuple[str, ...]]


@dataclass(frozen=True)
class PlanSignature:
    """编译 SQL 的结果形状签名 —— 照抄校验(全量编译)的判据。

    刻意**粗糙**:只留「改了就必然改变结果」的信号,把「等价改写」全部抹平,
    否则守卫会变成误伤机器。

    - ``projections``:每个投影的聚合函数名,无聚合写 ``None``。**不含列名**
      —— 编译器把 ``count(*)`` 归一到声明的 ``COUNT(col)`` 是合法等价。
    - ``tables``:FROM/JOIN 里直接引用的表名(升序)。
    - ``conds``:WHERE 的每个二元条件 ``(算子, 字面量元组)``。**不含列名**
      —— 过滤值一变签名必变,列重写不算偏离。
    - ``joins`` / ``groups``:JOIN 个数、GROUP BY 表达式个数。

    注意签名**与方言无关**:上面每项都在 parse 阶段定形,不随 read 方言变化
    (``DATE_FORMAT`` 与 ``date_trunc`` 一样只是"无聚合的投影")。所以契约按
    编译方言抽一次即可,校验侧不必再把两侧都 transpile 到 sqlite 对齐——
    "契约与生成 SQL 同方言"在 A1 之后是**结构保证**,不是运行时假设。
    """

    projections: tuple[str | None, ...] = ()
    tables: tuple[str, ...] = ()
    conds: tuple[tuple[str, tuple[str, ...]], ...] = ()
    joins: int = 0
    groups: int = 0


@dataclass(frozen=True)
class PlanContract:
    """编译器产出的权威交接。

    - ``skeleton_sql``:权威 SQL(全量编译 = 照抄;软 MISS = 骨架,只填缺)。
    - ``join_edges``:必须保留的 JOIN 边(集合语义;有序元组,见模块 docstring)。
    - ``where``:必须保留的过滤条件(集合语义)。
    - ``group_by_width``:必须保留的分组宽度。
    - ``signature``:全量编译的照抄判据(见 :class:`PlanSignature`)。``None``
      = 编译期抽取失败(编译器拼出的 SQL 解析不了,正常不该发生)—— 校验侧
      据此显式记录并放行,不伪装成"形状相同"。
    - ``gaps``:语义模型未覆盖、需生成通道自行补齐的组件。形状与
      ``PartialCompile.miss_parts`` 一致 —— 本来就是同一份数据
      (``{reason, component}``,两个键都由编译器写死,不是自由文本),
      不另立一层具名类型:多一层类型就多一次跨边界丢失的机会。
    - ``partial``:True = 软 MISS 骨架(投影允许补缺);False = 全量编译。
    """

    skeleton_sql: str
    join_edges: tuple[JoinEdge, ...] = ()
    where: tuple[WhereCond, ...] = ()
    group_by_width: int = 0
    signature: PlanSignature | None = None
    gaps: tuple[dict[str, str], ...] = ()
    partial: bool = False


# 两段固定文案:与 A0 之前 CompileResult/PartialCompile 里硬编码的字符串
# **逐字一致**(golden 测试锁住)。散文是渲染,不是真源 —— 改这里必须同时
# 想清楚消费方(gen_sql 的 system rule 引用了 "authoritative" 的语义)。
_FULL_HEADER = (
    "Compiled SQL (authoritative — generate exactly this SQL; only "
    "fix dialect or formatting if the schema demands it):"
)
_PARTIAL_HEADER = (
    "Compiled skeleton (authoritative — preserve these joins, filters and "
    "grouping exactly; only fill the gaps below):"
)
_GAPS_HEADER = (
    "Components NOT covered by the semantic model — generate these "
    "yourself from the query plan:"
)


def render_contract(contract: PlanContract) -> str:
    """契约 → 注入 gen_sql 的文本块(纯渲染,无信息增减)。

    编译块历来是英文(它紧跟在可能为中文的 plan 之后);语言不在这层分叉,
    因为契约里全是标识符与算子,自然语言部分只有上面三句固定文案。
    """
    lines = [_PARTIAL_HEADER if contract.partial else _FULL_HEADER]
    lines.append(f"```sql\n{contract.skeleton_sql}\n```")
    if contract.partial:
        lines.append(_GAPS_HEADER)
        for gap in contract.gaps:
            component = str(gap.get("component") or "").strip()
            suffix = f": {component}" if component else ""
            lines.append(f"- {gap.get('reason', '')}{suffix}")
    return "\n".join(lines)


def signature_to_wire(signature: PlanSignature | None) -> dict[str, Any] | None:
    """签名 → 纯 JSON(``None`` 原样透传:它是"抽取失败"的明确取值)。"""
    if signature is None:
        return None
    return {
        "projections": list(signature.projections),
        "tables": list(signature.tables),
        "conds": [
            {"op": op, "values": list(vals)} for op, vals in signature.conds
        ],
        "joins": signature.joins,
        "groups": signature.groups,
    }


def contract_to_wire(contract: PlanContract) -> dict[str, Any]:
    """契约 → 可跨节点边界的纯 JSON 形状(见模块 docstring)。

    只产出 str/int/bool/list/dict —— 不放 tuple/set/frozenset/自定义类型。
    ``test_contract_wire`` 有一条形状守卫测试,防止后续"顺手"改回去。
    """
    return {
        "skeleton_sql": contract.skeleton_sql,
        "join_edges": [[list(a), list(b)] for a, b in contract.join_edges],
        "where": [
            {"cols": [list(c) for c in cols], "op": op, "values": list(vals)}
            for cols, op, vals in contract.where
        ],
        "group_by_width": contract.group_by_width,
        "signature": signature_to_wire(contract.signature),
        "gaps": [dict(g) for g in contract.gaps],
        "partial": contract.partial,
    }


def _decode_col(raw: Any) -> Col | None:
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (str(raw[0]), str(raw[1]))
    return None


def _decode_edges(raw: Any) -> tuple[JoinEdge, ...] | None:
    if not isinstance(raw, list):
        return None
    out: list[JoinEdge] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            return None
        left, right = _decode_col(item[0]), _decode_col(item[1])
        if left is None or right is None:
            return None
        out.append((left, right))
    return tuple(out)


def _decode_where(raw: Any) -> tuple[WhereCond, ...] | None:
    if not isinstance(raw, list):
        return None
    out: list[WhereCond] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        cols_raw, op, vals_raw = item.get("cols"), item.get("op"), item.get("values")
        if not isinstance(cols_raw, list) or not isinstance(vals_raw, list):
            return None
        cols = [_decode_col(c) for c in cols_raw]
        if any(c is None for c in cols):
            return None
        out.append((
            tuple(c for c in cols if c is not None),
            str(op or ""),
            tuple(str(v) for v in vals_raw),
        ))
    return tuple(out)


def _decode_gaps(raw: Any) -> tuple[dict[str, str], ...] | None:
    if not isinstance(raw, list):
        return None
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        out.append({
            "reason": str(item.get("reason") or ""),
            "component": str(item.get("component") or ""),
        })
    return tuple(out)


def _decode_signature(raw: Any) -> PlanSignature | None | str:
    """签名解码。三态:``None``(确实没有签名)/ ``PlanSignature`` / 字符串
    ``"invalid"``(形状异常)。

    三态是必要的:``{"signature": null}`` 是合法且有意义的值(抽取失败),
    不能和"这个键根本没法解"混为一谈 —— 后者必须让整个契约作废。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "invalid"
    projections_raw = raw.get("projections", [])
    tables_raw = raw.get("tables", [])
    conds_raw = raw.get("conds", [])
    if not all(isinstance(x, list) for x in (projections_raw, tables_raw, conds_raw)):
        return "invalid"
    if not projections_raw:
        # 真实签名至少一个投影(SELECT 必有投影列表)。空投影只可能来自坏
        # wire 或坏编译器,放它过去 = 拿一个必然不匹配的判据去打回每一条生成
        # SQL —— 比缺签名危险得多。
        return "invalid"
    conds: list[tuple[str, tuple[str, ...]]] = []
    for item in conds_raw:
        if not isinstance(item, dict):
            return "invalid"
        vals = item.get("values")
        if not isinstance(vals, list):
            return "invalid"
        conds.append((str(item.get("op") or ""), tuple(str(v) for v in vals)))
    joins, groups = raw.get("joins", 0), raw.get("groups", 0)
    for n in (joins, groups):
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            return "invalid"
    return PlanSignature(
        projections=tuple(
            None if p is None else str(p) for p in projections_raw
        ),
        tables=tuple(str(t) for t in tables_raw),
        conds=tuple(conds),
        joins=joins,
        groups=groups,
    )


def contract_from_wire(wire: Any) -> PlanContract | None:
    """wire → 契约。**形状异常一律返回 ``None``**(不是"解出能解的部分")。

    部分解出的契约 = 被削弱的校验,那是 fail-open;``None`` 是明确的"没有
    契约",调用方退回老路径 —— 行为与契约缺席前逐字一致,不会静默放松。
    """
    if not isinstance(wire, dict):
        return None
    sql = wire.get("skeleton_sql")
    if not isinstance(sql, str) or not sql:
        return None
    join_edges = _decode_edges(wire.get("join_edges", []))
    where = _decode_where(wire.get("where", []))
    gaps = _decode_gaps(wire.get("gaps", []))
    if join_edges is None or where is None or gaps is None:
        return None
    width = wire.get("group_by_width", 0)
    if not isinstance(width, int) or isinstance(width, bool) or width < 0:
        return None
    signature = _decode_signature(wire.get("signature"))
    if isinstance(signature, str):  # "invalid"
        return None
    return PlanContract(
        skeleton_sql=sql,
        join_edges=join_edges,
        where=where,
        group_by_width=width,
        signature=signature,
        gaps=gaps,
        partial=bool(wire.get("partial")),
    )
