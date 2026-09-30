"""validator 档的执行器 —— 对查询结果做聚合级断言,零 LLM。

与 ``trove/workflow/rules.py`` 的关系:**平行,不是合并**。规则链
(``verify``)是"第一条失败即止、只回一个 reason",那是给**形状**用的
(单值问题的结果必须一行一列);validator 要的是**逐条判定 + 逐条
severity**(blocking 拦截 / advisory 只报告),一个 reason 装不下。
所以它是紧挨着 ``verify()`` 的**另一遍**,不是它的第 N 条规则。

三值是本模块的全部要点:``True`` 通过 / ``False`` 违反 /
``None`` **判不了**(缺列、Kleene Unknown、表达式异常)。把 None 塌成
True 是"没查却报平安",塌成 False 是"查不了却拦下正确结果" —— 两种都
比不检查更坏。这与 ``validate.py`` 里 ``plan_validation.status =
"untyped"`` 是同一条纪律:降级可以说,但不能不说。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from trove.core.logging import get_logger
from trove.services.decision.expr import (
    UNKNOWN,
    VALIDATOR_VARIABLES,
    DecisionExprError,
    as_number,
    parse_condition,
)

logger = get_logger(__name__)

#: 需要点名列才能算的标量。``row_count`` / ``col_count`` 不在此列 ——
#: 它们与列内容无关,永远可算。
AGGREGATES = ("min", "max", "sum", "avg", "null_count")

_AGGREGATE_KEYS = AGGREGATES


def _column_index(names: list[str], columns: list[str]) -> list[int]:
    return [i for i, c in enumerate(columns) if c in names]


def _numbers(rows: list[list], idx: int) -> list[float]:
    out: list[float] = []
    for r in rows:
        if idx < len(r):
            n = as_number(r[idx])
            if n is not None:
                out.append(n)
    return out


def build_scope(
    check: dict,
    columns: list[str],
    rows: list[list],
    row_count: int | None = None,
) -> dict[str, Any]:
    """一条 check 的求值作用域。

    - ``row_count`` / ``col_count`` 永远可算;``row_count`` 以调用方给的
      **权威值**为准(展示层可能截断 ``rows``)。
    - 点名的列**合并**计算(``min`` 对 ``{a,b}`` = ``min(min(a),min(b))``)
      —— "任意一列出现负值"正是想要的语义。
    - 点名的列一个都不在结果里 → 该标量取 ``UNKNOWN``(不是 0.0,也不是
      False)。``as_number`` 的 docstring 已经立过这条规矩:0.0 是自信的
      "肯定没触发",None 才正确地拒绝判断。
    - 非数值单元格跳过,不折算 —— ``"N/A"`` 不是 0。
    """
    names = list(check.get("columns") or [])
    idx = _column_index(names, columns)
    total = float(len(rows) if row_count is None else row_count)
    scope: dict[str, Any] = {
        "row_count": total,
        "col_count": float(len(columns)),
    }
    truncated = row_count is not None and total > len(rows)
    if not idx or truncated:
        # 无列可算(idx 空:没点名,或点名的列不在结果里),或结果集被截断
        # (``rows`` 只是展示窗口,``row_count`` 才是真实总数 —— 窗口内的
        # min/max/sum/avg/null_count 都不是整个结果的对应值,拿它报"通过"
        # 就是"没查却报平安")。两种都让聚合标量 UNKNOWN —— 谓词里用到它
        # 就是"判不了"。``row_count`` / ``col_count`` 与行内容无关,已在
        # 上面算好,不受影响。
        for k in _AGGREGATE_KEYS:
            scope[k] = UNKNOWN
        return scope
    scope["null_count"] = float(sum(
        1 for i in idx for r in rows
        if i >= len(r) or as_number(r[i]) is None
    ))
    pooled = [v for i in idx for v in _numbers(rows, i)]
    if pooled:
        scope["min"] = min(pooled)
        scope["max"] = max(pooled)
        scope["sum"] = sum(pooled)
        scope["avg"] = sum(pooled) / len(pooled)
    else:
        for k in ("min", "max", "sum", "avg"):
            scope[k] = UNKNOWN
    return scope


def run_validators(
    specs: list[dict],
    *,
    columns: list[str],
    rows: list[list],
    row_count: int | None = None,
    lang: str = "zh",
) -> list[dict[str, Any]]:
    """跑一遍 ``mode: deterministic`` 的 validator,逐条给判定。

    ``mode: llm`` 的条目**不在这里驱动**(需要 LLM 网关,由调用方决定是否
    启用),但**不会静默跳过** —— 它们落一条 ``verdict: None`` 并说明原因。
    静默跳过会让"配了却没人管"看起来和"检查通过"一样。

    单条 validator 内**第一条失败即止**(与规则链同一纪律:最具体的最先写),
    但不同 validator 之间互不影响 —— 每条都要出判定,而不是只报第一条。
    """
    out: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            out.append({
                "name": "",
                "verdict": None,
                "severity": "advisory",
                "message": "malformed validator spec (expected a mapping) — this validator did not run",
                "mode": "deterministic",
            })
            continue
        name = str(spec.get("name", ""))
        severity = str(spec.get("severity", "advisory"))
        mode = str(spec.get("mode") or "deterministic")
        if mode != "deterministic":
            # 本期只驱动 deterministic。**不 continue** —— 静默跳过等于
            # "声明了却没人管":文件在、确认过、什么都没发生,从外面看和
            # 检查通过一样。落一条 None(判不了),它不进用户附注(见
            # output.py::_validator_notice),但进 validator_hits 可供质检统计。
            out.append({
                "name": str(spec.get("name", "")),
                "verdict": None,
                "severity": str(spec.get("severity", "advisory")),
                "message": f"mode '{mode}' is not supported yet — this validator did not run",
                "mode": mode,
            })
            continue
        checks = spec.get("checks") or []
        if not isinstance(checks, Iterable):
            # 标量(``checks: 5`` / ``checks: yes``)是最省事的手写笔误,而手写
            # SKILL.md 正是绕开 create 校验的那条路 —— 直接迭代会抛 TypeError,
            # 抛出去就是每次查询都炸。降级为"判不了"。
            out.append({
                "name": name,
                "verdict": None,
                "severity": severity,
                "message": "malformed checks (expected a list) — this validator did not run",
                "mode": "deterministic",
            })
            continue
        if not checks:
            # 声明了却没有任何检查 = 判不了,不是通过。与上面 mode != deterministic
            # 同一条纪律:静默的"通过"和"没人管"从外面看一模一样。
            out.append({
                "name": name,
                "verdict": None,
                "severity": severity,
                "message": "no checks configured — this validator did not run",
                "mode": "deterministic",
            })
            continue
        verdict: bool | None = True
        message = ""
        for check in checks:
            try:
                if not isinstance(check, dict):
                    raise ValueError("malformed check (expected a mapping)")
                expr = str(check.get("expr") or "")
                fallback = str(check.get("message") or "")
                node = parse_condition(expr, VALIDATOR_VARIABLES)
                got = node.eval(build_scope(check or {}, columns, rows, row_count))
            except DecisionExprError as exc:
                verdict, message = None, f"bad expression: {exc}"
                break
            except Exception as exc:  # 表达式 bug 不得让管线崩
                logger.warning("validator %s check raised: %s", name, exc)
                verdict, message = None, f"check error: {exc}"
                break
            if got is UNKNOWN:
                verdict = None
                message = fallback or "cannot evaluate (missing column or non-numeric data)"
                break
            if got is not True:
                verdict = False
                message = fallback or f"violated: {expr}"
                break
        out.append({
            "name": name,
            "verdict": verdict,
            "severity": severity,
            "message": message,
            "mode": "deterministic",
        })
    return out
