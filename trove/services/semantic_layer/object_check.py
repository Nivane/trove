"""对象约束检查 —— 声明进生成侧的第一道确定性闸(零 LLM)。

定位:对象视图是**给人看的**投影,不改变准确率;本模块把同一份声明接到
**生成侧** —— 语义模型声明的多对多关系出现在聚合 SQL 的连接图上时,行倍增
会把 SUM/AVG 静默放大,而这正是编译器够不到的地带(软 MISS / 回退路径上
由 gen_sql 自由生成的 SQL,没有骨架保真校验兜底)。

判定与编译器/对象图**同源**:是否"行倍增边"直接复用
``object_graph._link_fan_out``(它再复用 ``compiler._is_many_to_many``)——
两处各写一遍判定,迟早漂移成「对象图页看着安全、生成侧被误拦」(或反过来)。

三条纪律,与 ``workflow/rules.py`` 的断言层一致:

* **纯函数、零 I/O、零 LLM**:输入 SQL + 上下文,输出 hits。解析失败 /
  上下文缺失 → 无命中 —— 判不了的 SQL 不拦:拦截的成本是把正确 SQL 打回
  重写,而遗漏只是不拦,两者不对称(rules.py 同款理由);
* **绝不抛**:任何内部异常折成"无命中"(守卫 bug 不许弄崩管线);
* **同输入逐字节同输出**:遍历顺序 = 文档顺序,输出不含集合迭代序。

消费点(同一实现,两处出口,零额外成本):

* ``workflow/nodes/validate.py`` —— 交付前拦截。与规则链**平行的一遍**
  (同 org validator 的理由:规则链签名统一为
  ``(question, sql, columns, rows, ...)``,语义声明进不去),命中走同一条
  ``error_feedback`` 通道与同一份修正预算;
* ``workflow/nodes/gen_sql.py::check_result`` —— agentic 生成循环内的即时
  反馈(模型定稿前自己看到违规,当场修正——比交付前打回省一整轮预算)。

F5-a 扇出守卫的触发面(**刻意收窄**,三条同时成立才拦):

  ① 同一 SELECT 层的直接数据源里,一条声明 M:N 边(且未声明 ``dedup``
     豁免)的两端表都在场 —— 该层的每一行都已被这次连接倍增;
  ② 该层投影里有非 DISTINCT 的 SUM/AVG(窗口聚合不算——它不折叠行;
     COUNT/MIN/MAX 不放大,不在其列);
  ③ 该层的 GROUP BY(若有)没有引用该边任一端点的声明键列。

  按声明键分组的查询不在其列(按实体分组的口径不受整行倍增影响);
  "先在子查询里聚合,再连接"的写法天然落在①之外(外层直接源是子查询,
  不是两张物理表)。已知范围边界:被 CTE/子查询包住的行倍增(内外层各
  看不出完整图景)判不了 → 不拦。

F5-b 声明键守卫 —— 编译期 ``ambiguous_join_path`` 硬 MISS 在生成侧的对位:

  两张表之间有声明的关系时,**声明键就是那条连接路径**。查询按声明键
  之外的列把它们连起来(ON / 顶层 WHERE 等值 / USING),连接的是语义
  模型从未认可的关系 —— 编译路径会被 join 修复/拒绝挡下,而软 MISS /
  回退路径上自由生成的 SQL 没有这层兜底。触发面(四条同时成立才拦):

  ① 同一 SELECT 层的直接数据源里,两张不同物理表都在场(自连接不算);
  ② 这两张表之间有声明的关系(任一方向;列齐全的边才可判);
  ③ 该层存在把它们等值相连的**连接等值**(ON / 顶层 AND 链 WHERE /
     USING)—— 没有等值(交叉连接、范围连接、NATURAL)不判,漏判只是
     不拦;
  ④ 没有任何一条连接等值命中声明键对(按 ``zip(from_columns,
     to_columns)`` 的位置配对,任一方向;USING(c) 命中要求 c 同时出现在
     两端声明列里)—— 只要有一条命中,这条路径就算用上了声明,放行。

  刻意不拦(已知边界,都偏"漏判"一侧):含 OR 的条件子树不下钻(不保证
  成立)、无限定符的列不参与认定(归属不明)、子查询/CTE 包住的连接内外层
  各看不出完整图景;USING 只在命中声明同名键时参与认定(`USING(c)` 等值
  的是右侧表与**整条左链**,左链可能含多张表,逐表归属无从判定 ——
  误归属会把正确查询打回,漏判只是不拦)。

  为什么"跨键连接"要拦:合法模式(如 financial 里 trans 与 disp 经
  account_id 的"塌缩连接")的前提是这两张表**没有**直接声明 —— 有声明
  时,声明就是唯一被认可的路径,跨键连接是未声明关系的近似(结果行数、
  口径都可能错)。声明是边界:要跨键,先补一条关系声明。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trove.core.i18n import L
from trove.services.semantic_layer.object_graph import _link_fan_out
from trove.services.semantic_layer.rls import physical_table

__all__ = [
    "DeclaredJoin",
    "FanoutEdge",
    "ObjectContext",
    "build_object_context",
    "context_from_provider",
    "check_object_sql",
]


@dataclass(frozen=True)
class FanoutEdge:
    """一条声明为行倍增的关系(M:N 且无 dedup 豁免;表名已归一为物理名)。"""

    name: str
    from_table: str
    to_table: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]

    @property
    def keys(self) -> frozenset[str]:
        """声明键列名集合(小写;两端合并——判"是否按声明键分组"用)。"""
        return frozenset(
            c.lower() for c in (*self.from_columns, *self.to_columns) if c
        )

    def keys_text(self) -> str:
        """声明键的可读形式(与 object_graph 的回退格式同款)。"""
        from_cols = ", ".join(self.from_columns)
        to_cols = ", ".join(self.to_columns)
        if from_cols or to_cols:
            return f"{from_cols} → {to_cols}"
        return f"{self.from_table} → {self.to_table}"


@dataclass(frozen=True)
class DeclaredJoin:
    """一条声明的连接路径(F5-b 用;含 1:N 与 dedup 边 —— 键的合法性
    与基数无关)。列不齐(任一端为空)的边在构造时被跳过:没有键就无从
    判定"连得对不对",不猜。"""

    name: str
    from_table: str
    to_table: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]

    def keys_text(self) -> str:
        """``from.col → to.col`` 可读形式(位置配对,与编译器 zip 同款)。"""
        left = ", ".join(f"{self.from_table}.{c}" for c in self.from_columns)
        right = ", ".join(f"{self.to_table}.{c}" for c in self.to_columns)
        return f"{left} → {right}"


@dataclass(frozen=True)
class ObjectContext:
    """检查所需的声明快照(纯数据;由 provider 一次性派生)。

    两份视图同源:``fanout_edges``(M:N 行倍增边,F5-a)与
    ``declared_joins``(全部声明的连接路径,F5-b)。两者都空 →
    ``build_object_context`` 返回 ``None``,检查整体静默。
    """

    fanout_edges: tuple[FanoutEdge, ...] = ()
    declared_joins: tuple[DeclaredJoin, ...] = ()


def build_object_context(model: Any | None) -> ObjectContext | None:
    """SemanticModel → 检查上下文;无可用声明 → ``None``(静默)。

    只提取检查需要的声明,不持有 provider / 模型对象本身 —— 上下文是
    一次问答里稳定的纯数据快照,检查函数因此可单测、可离线重放。
    """
    if model is None:
        return None
    phys = {d.name: physical_table(d) for d in model.datasets}
    edges: list[FanoutEdge] = []
    joins: list[DeclaredJoin] = []
    for r in model.relationships:
        # 数据集名 → 物理表名(与 RLS / 漂移报告同一份映射);
        # 数据集不在声明列表时按裸名兜底(列比对仍可能命中)。
        from_table = phys.get(r.from_, (r.from_ or "").lower())
        to_table = phys.get(r.to, (r.to or "").lower())
        from_cols = tuple(r.from_columns or ())
        to_cols = tuple(r.to_columns or ())
        # F5-b 视图:全部边(键合法性与基数无关),但列不齐的边不进
        # (没有键就无从判"连得对不对"——不猜)。
        if from_table and to_table and from_cols and to_cols:
            joins.append(DeclaredJoin(
                name=r.name, from_table=from_table, to_table=to_table,
                from_columns=from_cols, to_columns=to_cols,
            ))
        # F5-a 视图:只收行倍增边(M:N 且无 dedup 豁免)。
        if _link_fan_out(r):
            edges.append(FanoutEdge(
                name=r.name, from_table=from_table, to_table=to_table,
                from_columns=from_cols, to_columns=to_cols,
            ))
    if not edges and not joins:
        return None
    return ObjectContext(
        fanout_edges=tuple(edges), declared_joins=tuple(joins),
    )


def context_from_provider(provider: Any | None) -> ObjectContext | None:
    """SemanticLayerProvider → 上下文;读者缺席 / 读不出 → ``None``。

    provider.model() 自身承诺不把异常抛进问答流;这里再兜一层:对象检查
    是纯增益守卫,读不到声明就整段静默 —— 绝不因为"读语义模型"这件事
    本身把问题拦下(与 masking 的 deny 方向相反,这里的失败方向是放行)。
    """
    if provider is None:
        return None
    try:
        model = provider.model()
    except Exception:  # noqa: BLE001 —— provider 异常类型不承诺稳定
        return None
    try:
        return build_object_context(model)
    except Exception:  # noqa: BLE001 —— 守卫自身不许弄崩管线
        return None


# ── SQL 结构分析(私有;纯函数)───────────────────────────────────


def _parse(sql: str, dialect: str):
    """解析失败 → ``None``(判不了不拦)。"""
    try:
        from sqlglot import parse_one

        return parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return None


def _direct_sources(select, cte_names: set[str]) -> dict[str, str]:
    """一个 SELECT 层的直接数据源 ``{别名(小写): 物理表名(小写)}``。

    只认直接挂在该层的 ``exp.Table``(FROM + JOIN);子查询与 CTE 引用
    不是物理表,不进表 —— "先在子查询里聚合再连接"的写法因此天然免疫。
    库名/schema 前缀由 ``exp.Table.name`` 剥掉(与物理名归一同一口径)。
    """
    from sqlglot import exp

    sources: dict[str, str] = {}
    from_ = select.args.get("from_") or select.args.get("from")
    for node in [from_, *(select.args.get("joins") or [])]:
        inner = getattr(node, "this", None)
        if not isinstance(inner, exp.Table):
            continue
        name = (inner.name or "").lower()
        if not name or name in cte_names:
            continue
        sources[((inner.alias or inner.name) or "").lower()] = name
    return sources


def _group_columns(select) -> set[str]:
    """该层 GROUP BY 引用的列名(小写,裸名——限定符不参与键比对)。"""
    from sqlglot import exp

    group = select.args.get("group")
    if group is None:
        return set()
    return {c.name.lower() for c in group.find_all(exp.Column) if c.name}


def _inflatable_aggs(select) -> list:
    """该层会被行倍增放大的聚合:非 DISTINCT、非窗口的 SUM/AVG。

    只认"最近祖先 SELECT 就是本层"的节点(子查询里的聚合属于那一层,
    由那一层自己判)。COUNT/MIN/MAX 不在其列:计数与极值不因整行复制
    而放大(rules.py 的 F3-b 另从结果侧管重复)。
    """
    from sqlglot import exp

    out = []
    for agg in select.find_all(exp.Sum, exp.Avg):
        if agg.find_ancestor(exp.Select) is not select:
            continue
        if isinstance(agg.parent, exp.Window):
            continue
        if isinstance(agg.this, exp.Distinct):
            continue
        out.append(agg)
    return out


def _fanout_hit(
    select, ctx: ObjectContext, cte_names: set[str], lang: str,
) -> dict[str, Any] | None:
    """F5-a:该层连接了声明 M:N 边 + 有可放大聚合 + 未按声明键分组。"""
    sources = _direct_sources(select, cte_names)
    if len(sources) < 2:
        return None
    if not _inflatable_aggs(select):
        return None
    present = set(sources.values())
    group_cols = _group_columns(select)
    for edge in ctx.fanout_edges:
        if edge.from_table == edge.to_table:
            continue  # 自连接不构成边
        if edge.from_table not in present or edge.to_table not in present:
            continue
        keys = edge.keys
        if keys and (group_cols & set(keys)):
            continue  # 已按声明键分组 → 组内口径不受整行倍增影响
        return {
            "name": "F5-a",
            "reason": "[F5-a] " + L(
                lang,
                f"聚合(SUM/AVG)所在的查询连接了多对多关系「{edge.name}」"
                f"({edge.from_table} ↔ {edge.to_table},该连接会把每行成倍"
                f"复制),且未按该关系的声明键({edge.keys_text()})分组——"
                f"聚合结果会被扇出放大。改用 EXISTS/IN 子查询做过滤,或先在"
                f"子查询里按声明键去重/聚合一侧,再连接。",
                f"Aggregation (SUM/AVG) joins the many-to-many relationship "
                f"\"{edge.name}\" ({edge.from_table} ↔ {edge.to_table}); this "
                f"join duplicates rows and the query is not grouped by the "
                f"relationship's declared keys ({edge.keys_text()}), so the "
                f"aggregate will be inflated by the fan-out. Filter with "
                f"EXISTS/IN, or pre-aggregate/deduplicate one side on the "
                f"declared keys in a subquery before joining.",
            ),
        }
    return None


# ── F5-b 声明键守卫 ─────────────────────────────────────────


def _and_chain_eqs(node) -> list:
    """AND/Paren 链上的简单等值(EQ)。**含 OR 的子树一律不下钻** ——
    ``x = y OR p = q`` 不保证任何一条等值成立,把它当连接条件会误判;
    不下钻是"漏判不误拦"一侧。None → 空。"""
    from sqlglot import exp

    if node is None:
        return []
    if isinstance(node, exp.EQ):
        return [node]
    if isinstance(node, exp.And):
        return _and_chain_eqs(node.this) + _and_chain_eqs(node.expression)
    if isinstance(node, exp.Paren):
        return _and_chain_eqs(node.this)
    return []


def _linked_pair(eq, alias_map: dict[str, str]):
    """等值 → ``(左物理表, 右物理表, 左列, 右列)``;不是"两张在场表
    之间的列等值"(字面量/函数/无限定符/派生表列) → ``None``。

    无限定符的列归属不明(可属于任一张在场表)—— 不认定,偏漏判一侧。
    """
    from sqlglot import exp

    if not isinstance(eq, exp.EQ):
        return None
    left, right = eq.this, eq.expression
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return None
    lp = alias_map.get((left.table or "").lower())
    rp = alias_map.get((right.table or "").lower())
    if not lp or not rp or lp == rp:
        return None
    return lp, rp, left.name.lower(), right.name.lower()


def _eq_sanctions(edge: DeclaredJoin, lp: str, rp: str, lc: str, rc: str) -> bool:
    """这条等值是否命中声明键对(位置配对,任一方向)。"""
    if lp == edge.from_table and rp == edge.to_table:
        lcol, rcol = lc, rc
    elif lp == edge.to_table and rp == edge.from_table:
        lcol, rcol = rc, lc
    else:
        return False
    return any(
        lcol == fc.lower() and rcol == tc.lower()
        for fc, tc in zip(edge.from_columns, edge.to_columns)
    )


def _declared_hit(
    select, ctx: ObjectContext, cte_names: set[str], lang: str,
) -> dict[str, Any] | None:
    """F5-b:声明了关系的两张表被按非声明键连接 → 命中。"""
    alias_map = _direct_sources(select, cte_names)
    present = list(dict.fromkeys(alias_map.values()))  # 首见顺序,确定性
    if len(present) < 2:
        return None

    # 本层的连接等值:ON(AND 链)+ 顶层 WHERE(AND 链)。
    eqs: list[tuple] = []
    for join in select.args.get("joins") or []:
        for eq in _and_chain_eqs(join.args.get("on")):
            linked = _linked_pair(eq, alias_map)
            if linked is not None:
                eqs.append((*linked, eq.sql()))
    where = select.args.get("where")
    for eq in _and_chain_eqs(where.this if where is not None else None):
        linked = _linked_pair(eq, alias_map)
        if linked is not None:
            eqs.append((*linked, eq.sql()))

    # USING(c):右侧表与该层的左链表等值 —— 只认右侧表是一条声明边的
    # 端点、且 c 同时出现在两端声明列里(同名配对)的情形。
    using_links: list[tuple[str, str, str]] = []  # (右表, 另一端, c)
    for join in select.args.get("joins") or []:
        right = getattr(join.this, "name", None)
        right_phys = alias_map.get((right or "").lower())
        if not right_phys:
            continue
        for ident in join.args.get("using") or []:
            col = (getattr(ident, "name", "") or "").lower()
            if not col:
                continue
            for edge in ctx.declared_joins:
                other = None
                if right_phys == edge.from_table:
                    other = edge.to_table
                elif right_phys == edge.to_table:
                    other = edge.from_table
                if other is None or other == right_phys or other not in present:
                    continue
                if (col in {c.lower() for c in edge.from_columns}
                        and col in {c.lower() for c in edge.to_columns}):
                    using_links.append((right_phys, other, col))

    for i, p1 in enumerate(present):
        for p2 in present[i + 1:]:
            edges = [
                e for e in ctx.declared_joins
                if e.from_table != e.to_table
                and {e.from_table, e.to_table} == {p1, p2}
            ]
            if not edges:
                continue
            pair = {p1, p2}
            pair_eqs = [e for e in eqs if {e[0], e[1]} == pair]
            pair_using = [
                u for u in using_links if {u[0], u[1]} == pair
            ]
            if not pair_eqs and not pair_using:
                continue  # 没有等值相连(交叉/范围/NATURAL)→ 不判
            if any(
                _eq_sanctions(edge, e[0], e[1], e[2], e[3])
                for edge in edges for e in pair_eqs
            ) or pair_using:
                continue  # 有任一命中声明键对的等值(或声明的 USING)→ 放行
            edge = edges[0]
            # 报文里的"实际用的条件":文档顺序第一条(确定性;读起来也是
            # 查询自己先写的那个连接条件)。
            used = pair_eqs[0][4] if pair_eqs else f"USING ({pair_using[0][2]})"
            return {
                "name": "F5-b",
                "reason": "[F5-b] " + L(
                    lang,
                    f"查询把「{p1}」与「{p2}」按 {used} 连接,但语义模型对"
                    f"这两张表声明的关系是「{edge.name}」"
                    f"({edge.keys_text()})——这条连接路径没有声明。请按声明"
                    f"键连接;若确需别的路径,先把关系补进语义模型。",
                    f"The query joins \"{p1}\" and \"{p2}\" on {used}, but the "
                    f"semantic model declares their relationship "
                    f"\"{edge.name}\" via {edge.keys_text()} — this join path "
                    f"is undeclared. Join on the declared keys, or declare "
                    f"the relationship in the semantic model first.",
                ),
            }
    return None


def check_object_sql(
    sql: str,
    ctx: ObjectContext | None,
    *,
    lang: str = "zh",
    dialect: str = "",
) -> list[dict[str, Any]]:
    """对象约束检查(纯函数):SQL + 声明上下文 → hits(至多一条)。

    与规则链同纪律:**第一条命中的检查即止**(一个可执行的修复指向胜过
    一串);解析失败 / 无上下文 → 空列表(不拦)。返回形状与
    ``rules.verify`` 的 hits 一致(``[{name, reason}]``,reason 带
    ``[F5-x]`` 前缀)—— 下游(validation_hits / 评测归因)无需分支。
    """
    from sqlglot import exp

    if ctx is None or not sql:
        return []
    tree = _parse(sql, dialect)
    if tree is None:
        return []
    try:
        cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
        for select in tree.find_all(exp.Select):
            # 同一层内 F5-a 先判(静默放大比重罪):两者都成立时报扇出。
            for check in (_fanout_hit, _declared_hit):
                hit = check(select, ctx, cte_names, lang)
                if hit is not None:
                    return [hit]
    except Exception:  # noqa: BLE001 —— 守卫不许弄崩管线(同 rules.verify)
        return []
    return []
