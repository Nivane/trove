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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trove.core.i18n import L
from trove.services.semantic_layer.object_graph import _link_fan_out
from trove.services.semantic_layer.rls import physical_table

__all__ = [
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
class ObjectContext:
    """检查所需的声明快照(纯数据;由 provider 一次性派生)。

    空(fanout_edges 与 declared_joins 都空)→ ``build_object_context``
    返回 ``None``,检查整体静默 —— 未声明的部署逐字节不变。
    """

    fanout_edges: tuple[FanoutEdge, ...] = ()


def build_object_context(model: Any | None) -> ObjectContext | None:
    """SemanticModel → 检查上下文;无可用声明 → ``None``(静默)。

    只提取检查需要的声明,不持有 provider / 模型对象本身 —— 上下文是
    一次问答里稳定的纯数据快照,检查函数因此可单测、可离线重放。
    """
    if model is None:
        return None
    phys = {d.name: physical_table(d) for d in model.datasets}
    edges: list[FanoutEdge] = []
    for r in model.relationships:
        if not _link_fan_out(r):
            continue
        edges.append(FanoutEdge(
            name=r.name,
            # 数据集名 → 物理表名(与 RLS / 漂移报告同一份映射);
            # 数据集不在声明列表时按裸名兜底(列比对仍可能命中)。
            from_table=phys.get(r.from_, (r.from_ or "").lower()),
            to_table=phys.get(r.to, (r.to or "").lower()),
            from_columns=tuple(r.from_columns or ()),
            to_columns=tuple(r.to_columns or ()),
        ))
    if not edges:
        return None
    return ObjectContext(fanout_edges=tuple(edges))


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
            hit = _fanout_hit(select, ctx, cte_names, lang)
            if hit is not None:
                return [hit]
    except Exception:  # noqa: BLE001 —— 守卫不许弄崩管线(同 rules.verify)
        return []
    return []
