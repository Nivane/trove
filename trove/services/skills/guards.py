"""guard 档 —— **执行前**对 SQL 文本下断言(SQL 域的 validator 对称物)。

与 ``validators.py`` 的关系是**对称,不是复制**:validator 在 ``validate``
节点对**结果**下断言(执行后,零 LLM);guard 在 ``execute_sql`` 节点对
**SQL 文本**下断言(执行前,零 LLM)。同一套判定纪律(首败即停该条 /
``None`` 必带 reason 码 / 不许静默 / malformed spec 报 ``malformed_spec``)
只写一遍,在 ``validators.run_checks`` 里;本模块只提供 **SQL 域**的东西:
变量闭集、特征提取器、成因码与词表。

**极性(写守卫之前先读这一段)**:``expr`` 是**合规要求(断言)** —— 表达式
成立 = 合规,**不**成立 = 该守卫**命中**(``triggered is True``);blocking
档命中拦下重生成,advisory 档只报告。写守卫时脑子里想的是"合规的 SQL 长
什么样":``select_star == 0``(禁裸查)、``has_limit == 1``(明细必须限量)、
``table_count <= 4``(防 join 爆炸)。与 validator 的 ``verdict`` 语义同向
(True 通过 / False 违反),"对称物"就是这个意思。

**三值**:提取失败(语法坏句)→ ``GUARD_VARIABLES`` 全部 ``UNKNOWN`` →
判定 ``triggered is None`` → **绝不拦**。把 None 塌成"命中"会拦下正确结果,
塌成"通过"是"没查却报平安" —— 两者都比不检查更坏(与 validator 同一条
纪律)。

:func:`extract_sql_features` 与 :data:`GUARD_VARIABLES` 是**一对**:声明什么
就提取什么,键集一一对账(测试逐个钉住)。成功路径上每个变量都算得出来,
所以"某个变量 UNKNOWN ⇒ 解析失败"是 ``unparseable_sql`` 这条原因码的依据。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse_one

from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.services.decision.expr import UNKNOWN
from trove.services.skills.validators import SEVERITIES, run_checks

logger = get_logger(__name__)

#: guard 唯一的宿主节点:SQL 断言只在 ``execute_sql`` 里跑(紧接授权门
#: 之后、执行之前 —— 见 ``nodes/execute_sql.py``)。同 ``VALIDATOR_HOST``。
GUARD_HOST = "execute_sql"

#: 表达式标识符**闭集**(§3b 的九个 SQL-AST 变量)。写守卫时对域外标识符
#: ``parse_condition(expr, GUARD_VARIABLES)`` 直接 400(位置级报错,见
#: ``SkillService._validate_guard_spec``)—— 拼错的名字是**写时**错误,
#: 不是一条永远不命中的静默守卫。
#:
#: 全部是 **int**(0/1 或计数):``expr.as_number`` 对 ``bool`` 明确返回
#: ``None``(见 ``decision/expr.py``),提取器若产出 ``True``/``False``,
#: 每条守卫都会静默变成"判不了"。布尔语义用 0/1 表达。
GUARD_VARIABLES = frozenset({
    "select_star",         # 0/1 展开星号出现(``SELECT *``;``COUNT(*)`` 不算)
    "table_count",         # int FROM+JOIN 去重表数(CTE 别名不算表)
    "join_count",          # int JOIN(含逗号连接)出现次数
    "has_where",           # 0/1 存在 WHERE 且**非恒真**(``1=1``/``TRUE`` 不算)
    "has_limit",           # 0/1 有 LIMIT/FETCH
    "has_time_predicate",  # 0/1 WHERE/HAVING 引用日期/时间列或日期字面量
    "has_aggregate",       # 0/1 出现聚合函数(COUNT/SUM/AVG/MIN/MAX…)
    "cte_count",           # int WITH 子句数
    "sql_len",             # int 归一化(空白折叠)后的字符数
})

#: 守卫声明的回滚目标闭集。前三个是图上的**语义标签**(``analyze_error`` 的
#: 回滚梯子);``gen_retrieve`` 是默认值(§3d)—— 它不在梯子上,经
#: ``_resolve_rollback`` 归一到 ``gen_sql``,路由到同一个生成链入口
#: (``analyze_targets["gen_sql"] = "gen_retrieve"``),梯子升级语义不变。
GUARD_TARGETS = ("gen_retrieve", "gen_sql", "query_sketch", "schema_linking")
DEFAULT_GUARD_TARGET = "gen_retrieve"

#: ``triggered is None`` 的**机器可读**原因码闭集(SQL 域)。
#:
#: 前四条是配置侧(写时被 ``_validate_guard_spec`` 拦,手写 SKILL.md 绕过
#: create 时在这里降级);中间五条来自共用内核;最后一条是 SQL 域独有。
GUARD_NONE_REASONS = (
    "host_mismatch",        # 声明了非宿主 node(execute_sql 之外)
    "unknown_severity",     # severity 不在 SEVERITIES
    "unknown_target",       # target 不是 GUARD_TARGETS 里的回滚目标
    "missing_guard_block",  # tier: guard 却没有 guard: 块(手写文件)
    "malformed_spec",       # spec 不是 mapping
    "malformed_checks",     # checks 不是列表 / 元素不是 mapping
    "empty_checks",         # 声明了却没有任何检查
    "bad_expression",       # 表达式解析失败
    "check_error",          # 求值抛异常
    "unparseable_sql",      # SQL 解析失败 → 全部变量 UNKNOWN(绝不拦)
    "unknown_value",        # 以上都不是的未知源(兜底)
)

#: UNKNOWN 的兜底判词按原因分句 —— **每个 ``unknown_reason`` 会返回的码都
#: 必须在这里有一行**(共用内核取译文那一步在 ``try`` 之外,少一个键就是
#: KeyError 冲出管线)。配置侧原因(message 里已写明配置键名)不走这里。
_GUARD_UNKNOWN_TEXTS: dict[str, tuple[str, str]] = {
    "unparseable_sql": ("判不了（SQL 解析失败，无法提取结构特征）",
                        "cannot evaluate (the SQL could not be parsed)"),
    "unknown_value": ("判不了（无法求值）", "cannot evaluate"),
}

# 时间列判定:名字启发式 + 日期字面量 + 日期函数。**启发式宁可多认**(多认
# 只让 `has_time_predicate == 1` 类守卫更松,少认会把带时间过滤的合规 SQL
# 判成不合规 —— 误拦 vs 漏放,前者更贵)。
_TIME_COL_RE = re.compile(
    r"(?:^|_)(?:date|time|dt|ts|day|month|year|week|quarter|hour|minute|second)(?:_|$)"
    r"|_at$"
)
_DATE_LITERAL_RE = re.compile(
    r"^\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_TIME_FUNC_NAMES = frozenset({
    "EXTRACT", "DATE", "DATETIME", "TIMESTAMP", "NOW", "CURDATE", "CURTIME",
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_DATETIME",
    "LOCALTIME", "LOCALTIMESTAMP", "DATE_ADD", "DATE_SUB", "DATE_TRUNC",
    "DATE_DIFF", "DATEDIFF", "TIMESTAMPDIFF", "TIMESTAMPADD", "STR_TO_DATE",
    "FROM_UNIXTIME", "UNIX_TIMESTAMP", "TO_DATE", "TO_TIMESTAMP", "MAKEDATE",
    "LAST_DAY", "CONVERT_TZ", "YEAR", "MONTH", "DAY", "DAYOFWEEK", "DAYOFMONTH",
    "DAYOFYEAR", "WEEK", "WEEKOFYEAR", "QUARTER", "HOUR", "MINUTE", "SECOND",
})
_TEMPORAL_TYPES = frozenset({
    exp.DataType.Type.DATE, exp.DataType.Type.DATETIME,
    exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ,
    exp.DataType.Type.TIMESTAMPLTZ, exp.DataType.Type.TIME,
})


def _ancestors(node: exp.Expression) -> Iterator[exp.Expression]:
    """自近及远的祖先链(sqlglot 30 只有单层 ``.parent``,没有 ``.parents``)。"""
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _all_unknown() -> dict[str, Any]:
    return {name: UNKNOWN for name in GUARD_VARIABLES}


def extract_sql_features(sql: str, dialect: str = "") -> dict[str, Any]:
    """一次 parse → ``GUARD_VARIABLES`` 全量特征(单次解析,九个变量共用)。

    失败方向是这里唯一重要的决定:**坏句不是拦截理由**(语法由编译期与
    ``SQL_GUARD`` 挡,guard 只判结构规范),所以解析失败返回**全 UNKNOWN**
    而非一组 0 —— 一组 0 会把"没看懂"变成"没有星号、没有 WHERE"的自信
    判断,那正是最危险的那种错。全 UNKNOWN ⇒ 判定 ``None`` ⇒ 绝不拦。

    ``sql_len`` 也走这条线:解析失败是对**整条 SQL** 的陈述,保留一条失败
    路径才能让 ``unparseable_sql`` 这条原因码有确切含义。
    """
    if not (sql or "").strip():
        return _all_unknown()
    try:
        tree = parse_one(sql, read=dialect or None)
    except Exception as exc:  # 解析器的异常类型不止一种,一律降级
        logger.debug("guard feature extraction failed: %s", exc)
        return _all_unknown()
    if tree is None:          # 某些路径不抛异常,直接给 None
        return _all_unknown()

    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    tables = {
        t.name.rsplit(".", 1)[-1].lower()
        for t in tree.find_all(exp.Table)
        if t.name and t.name.rsplit(".", 1)[-1].lower() not in cte_names
    }
    where_nodes = [*tree.find_all(exp.Where), *tree.find_all(exp.Having)]
    return {
        # 展开星号 = 星号到根之间没有函数节点:``COUNT(*)``/``EXISTS(SELECT *)``
        # 里的星号不展开列,不算裸查。
        "select_star": int(any(
            not any(isinstance(p, exp.Func) for p in _ancestors(star))
            for star in tree.find_all(exp.Star)
        )),
        # 与 ``authz.enforcer.referenced_tables`` 同判据(去重裸表名,CTE
        # 别名不算表)—— 两处对"SQL 触及哪些表"必须一致,有对账测试钉住。
        "table_count": len(tables),
        # 逗号连接(``FROM a, b``)在 sqlglot 里也是 Join,一并计数。
        "join_count": len(list(tree.find_all(exp.Join))),
        "has_where": int(any(
            not _always_true(w.this) for w in tree.find_all(exp.Where)
        )),
        "has_limit": int(
            tree.find(exp.Limit) is not None or tree.find(exp.Fetch) is not None
        ),
        "has_time_predicate": int(any(_has_time_ref(n) for n in where_nodes)),
        "has_aggregate": int(tree.find(exp.AggFunc) is not None),
        "cte_count": len(list(tree.find_all(exp.CTE))),
        # 归一化 = 空白折叠(缩进/换行不进长度),与"模型风格差异"解耦。
        "sql_len": len(" ".join(sql.split())),
    }


def _always_true(cond: exp.Expression | None) -> bool:
    """``WHERE`` 条件是否**恒真**(``1=1`` / ``TRUE`` / 恒真合取)。

    只管常见恒真形态 —— 模板里保底拼接的 ``WHERE 1=1`` 是这类守卫的主要
    对象。认不出的形态一律当"真过滤"(宁可让 ``has_where`` 判 1)。
    """
    if cond is None:
        return True
    if isinstance(cond, exp.Paren):
        return _always_true(cond.this)
    if isinstance(cond, exp.Boolean):
        return bool(cond.this)
    if isinstance(cond, exp.And):
        return _always_true(cond.this) and _always_true(cond.expression)
    if isinstance(cond, exp.Literal):
        if cond.is_string:
            return False
        try:
            return float(cond.this) != 0.0
        except (TypeError, ValueError):
            return False
    if isinstance(cond, exp.EQ):
        left, right = cond.this, cond.expression
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return str(left.this) == str(right.this) and not left.is_string
    return False


def _has_time_ref(node: exp.Expression) -> bool:
    """WHERE/HAVING 子句里是否引用日期/时间(列名启发式 / 日期字面量 / 日期函数)。"""
    for col in node.find_all(exp.Column):
        if _TIME_COL_RE.search(col.name.lower()):
            return True
    for lit in node.find_all(exp.Literal):
        if lit.is_string and _DATE_LITERAL_RE.match(str(lit.this)):
            return True
    for fn in node.find_all(exp.Func):
        if isinstance(fn, exp.Cast):
            if fn.to.this in _TEMPORAL_TYPES:
                return True
            continue
        if isinstance(fn, exp.Extract):
            return True
        name = str(fn.this or "") if isinstance(fn, exp.Anonymous) else fn.sql_name()
        if name.upper() in _TIME_FUNC_NAMES:
            return True
    return False


@dataclass(frozen=True)
class GuardVerdict:
    """一条守卫判定 —— 对外的词表按 guard 档声明(``reason``/``hint``/``target``)。

    ``reason`` 是守卫作者写的**人话判词**(= 声明里的 ``reason``;内核内部
    叫 ``message``,不外露);``none_reason`` 只在 ``triggered is None`` 时
    有值,是机器可读的成因码(见 ``GUARD_NONE_REASONS``)。机器码叫
    ``none_reason`` 而不是 ``reason``:同名两义(人话 vs 码)是最容易被
    消费方读错的那类字段。
    """

    name: str
    severity: str
    triggered: bool | None
    reason: str
    hint: str = ""
    target: str = DEFAULT_GUARD_TARGET
    none_reason: str | None = None

    @property
    def blocking(self) -> bool:
        """命中且阻断 —— execute_sql 门只认这一个谓词。"""
        return self.triggered is True and self.severity == "blocking"

    def as_hit(self) -> dict[str, Any]:
        """落进 ``state.guard_hits`` 的形状(可序列化的纯数据)。"""
        hit: dict[str, Any] = {
            "name": self.name,
            "severity": self.severity,
            "triggered": self.triggered,
            "reason": self.reason,
            "hint": self.hint,
            "target": self.target,
        }
        if self.triggered is None:
            # 判不了才带机器码:判定过的 hit 没有"为什么"
            hit["none_reason"] = self.none_reason or "unknown_value"
        return hit


def format_guard_hit(hit: Mapping[str, Any]) -> str:
    """一条 guard 判定的人类可读渲染(错误文本与附注共用)。"""
    name = " ".join(str(hit.get("name") or "").split())
    reason = " ".join(str(hit.get("reason") or "").split())
    return f"[{name}] {reason}" if name else reason


def _guard_extra(spec: Mapping[str, Any], lang: str) -> dict[str, Any]:
    """hit 上的域特有键:本地化 hint + 回滚目标(预置门与共用心跳都用它)。"""
    if not isinstance(spec, Mapping):
        return {"hint": "", "target": DEFAULT_GUARD_TARGET}
    zh, en = spec.get("_hint") or ("", "")
    hint = L(lang, zh or en, en or zh) if (zh or en) else ""
    return {"hint": hint, "target": str(spec.get("_target") or DEFAULT_GUARD_TARGET)}


def _guard_pre_gate(
    spec: dict, name: str, severity: str,
) -> dict[str, Any] | None:
    """guard 的域特有门:非宿主 → 未知 severity → 未知 target → 缺 guard 块。

    四条都发生在逐条求值之前,都是"声明了但引擎不会执行"的降级记录 ——
    宽容手写文件的出口是**可观测的 ``triggered is None``**,不是静默跳过。
    这几条诊断保持英文:逐字引用配置键名,更接近错误码。
    """
    mismatch = spec.get("host_mismatch")
    if mismatch:
        return {
            "name": str(spec.get("name", "")),
            "verdict": None,
            "reason": "host_mismatch",
            # 没运行的判定不能拦、也不能渲染 —— 用非阻断默认档。
            "severity": "advisory",
            "message": (
                f"triggers.node '{mismatch}' is not the guard host "
                f"('{GUARD_HOST}') — SQL assertions never run there, "
                "so this guard did not run"
            ),
            **_guard_extra(spec, "en"),
        }
    if severity not in SEVERITIES:
        # 未知 severity 的命中两头都接不住:execute_sql 要 "blocking" 才拦,
        # output 要 "advisory" 才渲染 —— 一条确定违反的风控口径静默消失。
        return {
            "name": name,
            "verdict": None,
            "reason": "unknown_severity",
            "severity": "advisory",
            "message": f"unknown severity '{severity}' — this guard did not run",
            **_guard_extra(spec, "en"),
        }
    target = str(spec.get("_target") or DEFAULT_GUARD_TARGET)
    if target not in GUARD_TARGETS:
        # 目标名打错会让 _extract_rollback_target 归一到梯子首档(gen_sql),
        # 实际的纠正方向与作者声明的不同 —— 记一条"判不了"而不是照跑。
        return {
            "name": name,
            "verdict": None,
            "reason": "unknown_target",
            "severity": severity,
            "message": (
                f"unknown rollback target '{target}' "
                f"(expected one of {', '.join(GUARD_TARGETS)}) — this guard did not run"
            ),
            **_guard_extra(spec, "en"),
        }
    if spec.get("_no_guard_block"):
        # tier: guard 却没有 guard: 块(手写文件):文件在、确认过、什么都没
        # 发生 —— 从外面看和检查通过一样。落一条可观测的"判不了"。
        return {
            "name": str(spec.get("name", "")),
            "verdict": None,
            "reason": "missing_guard_block",
            "severity": severity,
            "message": "tier is 'guard' but no guard: block is declared — this guard did not run",
            **_guard_extra(spec, "en"),
        }
    return None


def _guard_specs(entries: list, ) -> list[dict]:
    """把 org 技能条目展开成**守卫单元**:每条 authored check = 一个独立单元。

    守卫的声明是"一个 skill 带 N 条 checks"(每条自带 name/severity/reason/
    hint),而共用内核的纪律是**按 spec** 的那四条(首败即停 / 互不影响 /
    None 带码 / malformed)。把每条 check 映射成一个单检查 spec,四条纪律就
    按"每条守卫"原样成立 —— 一条守卫判不了不影响另一条。
    """
    specs: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            specs.append(entry)      # 非 mapping 条目 → 内核 malformed_spec 出口
            continue
        skill_name = str(entry.get("name") or "")
        guard = entry.get("guard")
        if not isinstance(guard, dict):
            specs.append({
                "name": skill_name, "severity": "advisory", "checks": [],
                "_no_guard_block": True,
            })
            continue
        checks = guard.get("checks")
        if not isinstance(checks, list) or not checks:
            # 形状不对 / 空 checks:原样交给内核的 malformed_checks / empty_checks
            specs.append({
                "name": skill_name,
                "severity": str(guard.get("severity") or "advisory"),
                "checks": checks,
            })
            continue
        for check in checks:
            if not isinstance(check, dict):
                specs.append({
                    "name": skill_name,
                    "severity": str(guard.get("severity") or "advisory"),
                    "checks": [check],
                })
                continue
            spec: dict[str, Any] = {
                "name": str(check.get("name") or "") or skill_name,
                "severity": str(check.get("severity") or "advisory"),
                # authored reason → 内核 message(内核的 message 就是"判词")
                "checks": [{"expr": check.get("expr"), "message": check.get("reason")}],
                "_hint": (str(check.get("hint_zh") or ""), str(check.get("hint_en") or "")),
                "_target": str(
                    check.get("target") or guard.get("target") or ""
                ) or DEFAULT_GUARD_TARGET,
                "_skill": skill_name,
            }
            if "host_mismatch" in entry:
                spec["host_mismatch"] = entry["host_mismatch"]
            specs.append(spec)
    return specs


def run_guards(
    entries: list,
    *,
    sql: str,
    dialect: str = "",
    lang: str = "zh",
) -> list[GuardVerdict]:
    """跑一遍 guard 档条目(``guards_for`` 交出来的已确认清单)。

    判定纪律全在 ``validators.run_checks``;这里只给 **SQL 域**:变量闭集
    ``GUARD_VARIABLES``、作用域 ``extract_sql_features``、成因 ``unparseable_sql``。
    零 LLM、零执行:只看 SQL 文本的结构。
    """
    features = extract_sql_features(sql, dialect)
    hits = run_checks(
        _guard_specs(entries),
        variables=GUARD_VARIABLES,
        scope_for=lambda check: features,
        # 成功路径上每个变量都算得出来 ⇒ 任一变量 UNKNOWN ⇔ 解析失败。
        unknown_reason=lambda check: "unparseable_sql",
        unknown_texts=_GUARD_UNKNOWN_TEXTS,
        label="guard",
        lang=lang,
        pre_gate=_guard_pre_gate,
        extra_hit=lambda spec, verdict: _guard_extra(spec, lang),
    )
    out: list[GuardVerdict] = []
    for hit in hits:
        verdict = hit.get("verdict")
        out.append(GuardVerdict(
            name=str(hit.get("name") or ""),
            severity=str(hit.get("severity") or "advisory"),
            # 断言不成立 ⇒ 命中;判不了(None)原样保留为 None —— 绝不拦。
            triggered=None if verdict is None else verdict is not True,
            reason=str(hit.get("message") or ""),
            hint=str(hit.get("hint") or ""),
            target=str(hit.get("target") or DEFAULT_GUARD_TARGET),
            none_reason=hit.get("reason"),
        ))
    return out


class GuardRunner:
    """``execute_sql`` 侧的门面:选人(``guards_for``)与判定(``run_guards``)分开。

    与 ``_build_authorizer`` 同一条接线纪律:``SkillService`` 缺席时
    ``_build_guards`` 返回 ``None``,execute_sql 里的 ``guards is not None``
    短路让**无守卫路径逐字节不变**(这是"加门不改路"的安全带)。
    """

    def __init__(self, skills: Any, *, host: str = GUARD_HOST) -> None:
        self._skills = skills
        self._host = host

    def check(self, sql: str, *, dialect: str = "", **ctx: Any) -> list[GuardVerdict]:
        """对一条待执行的 SQL 跑全部 guard 档守卫(``ctx`` 同 ``skill_ctx()``)。"""
        entries = self._skills.guards_for(self._host, **ctx) or []
        return run_guards(
            entries, sql=sql, dialect=dialect, lang=str(ctx.get("lang") or "zh"),
        )
