"""metric 表达式 → 组件树(纯函数,只读语义模型,零 I/O)。

两个用途:
1. ``metric_ratio_parts`` —— 比率识别(从 attribution 节点原样升格,
   行为逐字不变):AVG(x)→SUM/COUNT、A/B、SAFE_DIVIDE(A,B)。
2. ``metric_components`` —— **驱动器树**骨架:沿表达式结构分解为
   组件,供"指标变了,是哪个组件变了"的回答。

分解纪律:
- ``Add``/``Sub`` → ``derived``,**decomposable=True** —— Δ(a±b) 恒等于
  Δa±Δb,加性链上的分解是恒等式;
- ``Div``/``SafeDivide`` → ``ratio``、``Mul`` → ``derived`` 但
  **decomposable=False** —— 乘除链上子项变化量之和 ≠ 父变化量,
  不许硬拆(宁可不拆,不造恒等式);
- ``Avg(x)`` → ratio(SUM(x)/COUNT(x)),与 ``metric_ratio_parts`` 同构;
- 裸标识符若是已声明 metric 名 → 递归展开(环停、深度上限);
- 其余聚合函数(SUM/COUNT/MIN/MAX…)是叶子:可执行候选取表达式本身
  (编译器的聚合签名路径按签名解析回声明度量);
- 认不出的结构 → 叶子 + note="unresolved"(信息性,不执行)。

每个节点的 ``candidate`` 是"可执行候选"——喂给
``build_and_compile`` 多度量查询的名字;None = 不可执行(仅展示)。
``decomposable`` 是节点自身的性质,不继承。
"""

from __future__ import annotations

from typing import Any

#: 表达式文本的长度闸:超长表达式(生成/拼接产物)不展开,按叶子处理。
_MAX_EXPR_LEN = 4000


def metric_ratio_parts(metric: Any) -> tuple[str, str] | None:
    """比率度量 → (分子, 分母) 聚合表达式;加性/不可分解 → None。

    识别:metric_type == ratio,或表达式是 AVG(x)(→ SUM(x)/COUNT(x))、
    A/B 除法、SAFE_DIVIDE(A, B)。Paren 解包后判定。表达式为表限定
    (``students.grade``),产物可直接拼进 hop SQL。
    """
    if metric is None:
        return None
    expr = str(getattr(metric, "expression", "") or "").strip()
    if not expr:
        return None
    try:
        from sqlglot import exp, parse_one
        tree = parse_one(expr)
        while isinstance(tree, exp.Paren):
            tree = tree.this
        if isinstance(tree, exp.Avg):
            arg = tree.this.sql()
            return f"SUM({arg})", f"COUNT({arg})"
        if isinstance(tree, exp.Div):
            return tree.this.sql(), tree.expression.sql()
        if isinstance(tree, exp.SafeDivide):
            return tree.this.sql(), tree.expression.sql()
    except Exception:
        return None
    return None


def _leaf(expr_text: str, *, candidate: str | None = None, note: str | None = None) -> dict[str, Any]:
    return {
        "name": expr_text[:80],
        "kind": "leaf",
        "op": None,
        "expression": expr_text,
        "metric": None,
        "candidate": candidate,
        "decomposable": False,
        "children": [],
        "note": note,
    }


def _node(kind: str, op: str, expression: str, decomposable: bool, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": expression[:80],
        "kind": kind,
        "op": op,
        "expression": expression,
        "metric": None,
        "candidate": None,
        "decomposable": decomposable,
        "children": children,
        "note": None,
    }


def metric_components(metric: Any, model: Any, *, max_depth: int = 5) -> dict[str, Any]:
    """已声明 metric → 组件树骨架(纯函数,不取数)。

    ``model`` 提供度量名索引(裸标识符递归展开用)。表达式解析失败/
    超长 → 单叶子(根自身可执行)。展开深度按"进入被引用度量的表达式"
    计数,上限 ``max_depth``;命名环(metric 互引)在该引用处截停并标
    ``note="cycle"``。
    """
    name = str(getattr(metric, "name", "") or "")
    expr = str(getattr(metric, "expression", "") or "").strip()
    if not expr or len(expr) > _MAX_EXPR_LEN:
        return _leaf(expr or name, candidate=name or None,
                     note=None if expr else "empty_expression")

    by_name = {
        m.name.strip().lower(): m
        for m in (getattr(model, "metrics", None) or [])
        if str(getattr(m, "name", "") or "").strip()
    }

    def walk(expr_text: str, depth: int, path: set[str]) -> dict[str, Any]:
        try:
            from sqlglot import exp, parse_one
            tree = parse_one(str(expr_text))
        except Exception:
            return _leaf(str(expr_text), note="unparseable")
        while isinstance(tree, exp.Paren):
            tree = tree.this
        sql = tree.sql()
        if len(sql) > _MAX_EXPR_LEN:
            return _leaf(sql, note="too_large")

        if isinstance(tree, exp.Avg):
            arg = tree.this.sql()
            return _node(
                "ratio", "avg", sql, False,
                [_leaf(f"SUM({arg})", candidate=f"SUM({arg})"),
                 _leaf(f"COUNT({arg})", candidate=f"COUNT({arg})")],
            )
        if isinstance(tree, exp.Div) or isinstance(tree, getattr(exp, "SafeDivide", ())):
            return _node("ratio", "/", sql, False, [
                walk(tree.this, depth, path),
                walk(tree.expression, depth, path),
            ])
        if isinstance(tree, (exp.Add, exp.Sub)):
            op = "+" if isinstance(tree, exp.Add) else "-"
            return _node("derived", op, sql, True, [
                walk(tree.this, depth, path),
                walk(tree.expression, depth, path),
            ])
        if isinstance(tree, exp.Mul):
            return _node("derived", "*", sql, False, [
                walk(tree.this, depth, path),
                walk(tree.expression, depth, path),
            ])
        if isinstance(tree, exp.Column):
            ref = by_name.get(tree.name.strip().lower())
            if ref is not None:
                key = ref.name.strip().lower()
                if key in path:
                    return {**_leaf(sql, candidate=ref.name), "metric": ref.name, "note": "cycle"}
                if depth >= max_depth:
                    return {**_leaf(sql, candidate=ref.name), "metric": ref.name, "note": "depth"}
                sub = walk(str(getattr(ref, "expression", "") or ""), depth + 1, path | {key})
                return {**sub, "name": ref.name, "metric": ref.name, "candidate": ref.name}
        if isinstance(tree, exp.AggFunc):
            return _leaf(sql, candidate=sql)
        return _leaf(sql, note="unresolved")

    root = walk(expr, 0, {name.strip().lower()} if name else set())
    return {**root, "name": name or root["name"], "metric": name or None,
            "candidate": name or root.get("candidate")}


def collect_components(
    root: dict[str, Any],
    *,
    max_components: int = 4,
    exclude: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """树里可执行组件(preorder,按候选去重,排除根候选)→ (组件, 截断数)。

    只收集 ``candidate`` 非空的节点——它们是"能单独取到值"的组件。
    ``decomposable=False`` 的节点其子组件同样收集(比率根的分子/分母
    就是这种情况:父不可加,子各自有值仍然有用)。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    skip = {exclude.strip().lower()} if exclude else set()

    def visit(node: dict[str, Any]) -> None:
        for ch in node.get("children") or []:
            cand = str(ch.get("candidate") or "").strip()
            key = cand.lower()
            if cand and key not in seen and key not in skip:
                seen.add(key)
                out.append(ch)
            visit(ch)

    visit(root)
    truncated = max(0, len(out) - max_components)
    return out[:max_components], truncated
