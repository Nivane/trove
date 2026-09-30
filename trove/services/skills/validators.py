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

from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.services.decision.expr import (
    UNKNOWN,
    VALIDATOR_VARIABLES,
    DecisionExprError,
    as_number,
    parse_condition,
)

logger = get_logger(__name__)

#: 引擎**能执行**的 ``severity`` 闭集。与 ``service.py`` 的写入校验共用同一个
#: 定义 —— 两处各写一份的话,"写入放行、执行侧当未知"这条漂移会重新打开
#: "配了却静默失效"那类事故(``severity: Blocking`` 既不等于 blocking 也不等于
#: advisory,``validate.py`` 不拦、``output.py`` 不渲染,连一条 ``verdict: None``
#: 的质检记录都不落)。写入面与消费面必须看同一份名单。
SEVERITIES = ("blocking", "advisory")

#: validator 唯一的宿主节点:本期 ``targets`` 只有 ``result``,而结果断言只在
#: ``validate`` 节点运行。见 ``validators_for`` 对 node 触发条件的处置。
VALIDATOR_HOST = "validate"

#: 需要点名列才能算的标量。``row_count`` / ``col_count`` 不在此列 ——
#: 它们与列内容无关,永远可算。
AGGREGATES = ("min", "max", "sum", "avg", "null_count")

#: ``verdict is None`` 的**机器可读**原因码闭集。
#:
#: None 比率是这套机制唯一的质量信号("配了却判不了"的占比),而自由文本
#: message 数不出分布 —— 每一种 None 各有各的处置(手改文件写错 / 声明了
#: 非宿主 / 结果超出窗口 / 真缺列 / 这次查询本来就没结果),混在一句话里
#: 只能靠猜。每条 None 都必须带上它为什么判不了。
#:
#: **不写总数**:这张表会随 ``build_scope`` 的退化路径增删,而注释里的数字
#: 是最容易腐烂的那类事实(它离真相只有两行,却没有任何测试看着)。
NONE_REASONS = (
    "malformed_spec",       # spec 不是 mapping
    "host_mismatch",        # 声明了非宿主 node
    "unknown_severity",     # severity 不在闭集
    "unsupported_mode",     # mode 不是 deterministic(本期只驱动这一种)
    "malformed_checks",     # checks 不是列表 / 元素不是 mapping
    "empty_checks",         # 声明了却没有任何检查
    "bad_expression",       # 表达式解析失败
    "check_error",          # 求值抛异常
    "missing_column",       # 点名的列不在结果里
    "no_columns_declared",  # 谓词用到聚合,却没声明 columns
    "truncated_rows",       # 结果被展示窗口截断,窗口内的聚合不算数
    "empty_result",         # 结果集一行都没有(这次查询本来就没结果)
    "non_numeric_data",     # 列在、行也在,但拿不出可用的数值
    "unknown_value",        # 以上都不是的未知源(兜底)
)

#: UNKNOWN 的兜底判词按原因分句。原先一句"缺列或非数值数据"盖住四种,截断
#: 也被说成缺列 —— 运维照着它去查一个并不缺失的列。只覆盖数据侧原因
#:(截断 / 空结果 / 缺列 / 没声明列 / 非数值);配置侧原因(message 里已写明,
#: 且本就带着配置键名)不走这里。
#:
#: **每个 ``_unknown_reason`` 会返回的码都必须在这里有一行** —— ``run_validators``
#: 取译文那一步在 ``try`` 之外,少一个键就是 KeyError 冲出管线(不是降级)。
#: ``test_unknown_reason_tracks_scope_degradation`` 逐个断言这件事。
_UNKNOWN_TEXTS: dict[str, tuple[str, str]] = {
    "missing_column": ("判不了（结果里没有点名的列）",
                       "cannot evaluate (named column is not in the result)"),
    "no_columns_declared": ("判不了（这条检查没有声明 columns）",
                            "cannot evaluate (no columns declared on this check)"),
    "truncated_rows": ("判不了（结果集超出展示窗口，未在窗口内取值）",
                       "cannot evaluate (result set exceeds the display window)"),
    "empty_result": ("判不了（结果集为空，没有可读的行）",
                     "cannot evaluate (the result set is empty)"),
    "non_numeric_data": ("判不了（列里没有可用的数值）",
                         "cannot evaluate (no numeric data in the named columns)"),
    "unknown_value": ("判不了（无法求值）", "cannot evaluate"),
}

_AGGREGATE_KEYS = AGGREGATES


def format_hit(hit: dict[str, Any]) -> str:
    """一条判定的人类可读渲染 —— 用户屏幕的引用块与 ``error_feedback`` 共用。

    两条纪律都在这里:

    - **没有名字的 hit 不留 ``[]``** —— malformed spec 与手写文件漏 ``name``
      都会走到这里,``[] 消息`` 是残缺的排版;
    - **判词里的换行压平** —— 它会被拼进 Markdown 引用块,一个换行就把引用块
      冲出三行,把后面的内容顶成正文。
    """
    name = " ".join(str(hit.get("name") or "").split())
    message = " ".join(str(hit.get("message") or "").split())
    return f"[{name}] {message}" if name else message


def _unknown_reason(
    check: dict, columns: list[str], rows: list[list], row_count: int | None,
) -> str:
    """谓词算出 UNKNOWN 时,``build_scope`` 是**为什么**退化的。

    分支顺序 = 修复顺序:**配置问题**先于数据问题(缺列 / 没声明列是管理员
    改一行就能解决的事;截断是预期内的,不用改)。与 ``build_scope`` 的退化
    条件必须一一对应 —— ``test_unknown_reason_tracks_scope_degradation``
    把两侧钉在一起。
    """
    names = list(check.get("columns") or [])
    if names and not _column_index(names, columns):
        return "missing_column"
    if not names:
        return "no_columns_declared"
    if row_count is not None and float(row_count) > len(rows):
        return "truncated_rows"
    if not rows:
        # "一行都没有"与"列里全是非数值"是两回事:前者是这次查询本来就
        # 没结果(**常见,且不用改配置**),后者才指向列或数据。合成一格,
        # 日常空结果会把这个桶灌满 —— 而 None 的原因分布是这套机制唯一的
        # 质量信号,一个被日常噪声灌满的桶等于没有信号。截断同理(上一行)。
        return "empty_result"
    if not any(_numbers(rows, i) for i in _column_index(names, columns)):
        return "non_numeric_data"
    return "unknown_value"


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

    这里置 ``UNKNOWN`` 的每个条件,``_unknown_reason`` 那边都有一个分支
    对应(它负责回答"为什么判不了")。改这里的退化条件就要改那边 ——
    ``test_unknown_reason_tracks_scope_degradation`` 两侧一起钉。
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

    **不变量(四个入口同一条纪律)**:一切"声明了但引擎不会执行"的配置,
    要么写入时拒掉(400,见 ``SkillService._validate_validator_spec``),要么
    在这里降级成 ``verdict: None`` 落进 ``validator_hits`` —— 不许有第三种
    结局(静默)。今天对齐到这条的有:未知 ``mode`` / 畸形 ``checks`` / 空
    ``checks`` / 非宿主 ``triggers.node`` / 未知 ``severity``。

    **每条 ``None`` 都带 ``reason``**(见 ``NONE_REASONS``):None 比率是这套
    机制唯一的质量信号,而自由文本数不出分布。"配了却判不了"的每一种成因
    各有各的处置 —— 配置写错(管理员改一行)、结果超出窗口或**这次查询本来
    就没有结果**(两者都预期内,不用改配置)混在一句话里,运维只能靠猜。
    原因码只在 ``verdict is None`` 时出现:判定过的 hit 没有"为什么"。

    ``lang`` 作用于**兜底判词**(需要本地化的自由文本),它们有两类读者:
    用户屏幕上的 advisory 附注,与管理端看到的 ``validator_hits``。逐字引用
    配置键名的那几条诊断(mode / severity / host / 畸形 checks)保持英文 ——
    它们更接近错误码。
    """
    out: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            out.append({
                "name": "",
                "verdict": None,
                "reason": "malformed_spec",
                "severity": "advisory",
                "message": "malformed validator spec (expected a mapping) — this validator did not run",
                "mode": "deterministic",
            })
            continue
        name = str(spec.get("name", ""))
        # ``or`` 而不是 ``get(k, default)``:``severity:`` 写空(YAML null)拿到的是
        # 字符串 ``"None"``,那是个**未知**值,会把一条默认档的检查变成静默死档。
        severity = str(spec.get("severity") or "advisory")
        mismatch = spec.get("host_mismatch")
        if mismatch:
            # ``validators_for`` 标记的"声明了非宿主 node"—— 结果断言只在
            # ``VALIDATOR_HOST`` 跑,这份文件永远不会运行。把它丢在选人那一步,
            # 从任何外部面(附注、``validator_hits``、``list_org``)看都和"没写"
            # 一模一样,这正是本模块存在的理由所针对的那类事故。落一条带原因的
            # "判不了":可观测,不进附注。
            out.append({
                "name": str(spec.get("name", "")),
                "verdict": None,
                "reason": "host_mismatch",
                # 没运行的判词不能拦、也不能渲染 —— 用非阻断的默认档保证。
                "severity": "advisory",
                "message": (
                    f"triggers.node '{mismatch}' is not the validator host "
                    f"('{VALIDATOR_HOST}') — result assertions never run there, "
                    "so this validator did not run"
                ),
                "mode": "deterministic",
            })
            continue
        if severity not in SEVERITIES:
            # 未知 severity 的 verdict 是**明确的 False**,却两头都接不住:
            # ``validate.py`` 要 ``severity == "blocking"`` 才拦,``output.py`` 要
            # ``"advisory"`` 才渲染 —— 一条确定违反了的风控口径就这样静默消失。
            # 与 mode / 畸形 checks 同一处置:降级为"判不了",进 validator_hits
            # (可观测)不进附注(不进用户屏幕)。
            out.append({
                "name": name,
                "verdict": None,
                "reason": "unknown_severity",
                "severity": "advisory",
                "message": f"unknown severity '{severity}' — this validator did not run",
                "mode": "deterministic",
            })
            continue
        mode = str(spec.get("mode") or "deterministic")
        if mode != "deterministic":
            # 本期只驱动 deterministic。**不 continue** —— 静默跳过等于
            # "声明了却没人管":文件在、确认过、什么都没发生,从外面看和
            # 检查通过一样。落一条 None(判不了),它不进用户附注(见
            # output.py::_validator_notice),但进 validator_hits 可供质检统计。
            out.append({
                "name": str(spec.get("name", "")),
                "verdict": None,
                "reason": "unsupported_mode",
                "severity": severity,      # 上面已归一 + 校验过,不再各读一次
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
                "reason": "malformed_checks",
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
                "reason": "empty_checks",
                "severity": severity,
                "message": "no checks configured — this validator did not run",
                "mode": "deterministic",
            })
            continue
        verdict: bool | None = True
        reason: str | None = None
        message = ""
        for check in checks:
            if not isinstance(check, dict):
                # 形状守卫在 try **之外**:它不是"求值抛异常",是配置写错了
                # (``checks`` 写成映射时迭代出的是键)。原因码要分得开。
                verdict, reason = None, "malformed_checks"
                message = "malformed check (expected a mapping)"
                break
            try:
                expr = str(check.get("expr") or "")
                fallback = str(check.get("message") or "")
                node = parse_condition(expr, VALIDATOR_VARIABLES)
                got = node.eval(build_scope(check or {}, columns, rows, row_count))
            except DecisionExprError as exc:
                verdict, reason = None, "bad_expression"
                message = f"bad expression: {exc}"
                break
            except Exception as exc:  # 表达式 bug 不得让管线崩
                logger.warning("validator %s check raised: %s", name, exc)
                verdict, reason = None, "check_error"
                message = f"check error: {exc}"
                break
            # 这一段的两条兜底判词跟着 ``lang`` 走,但**去处在两个不同的地方**,
            # 别把它们混作一谈:
            # - ``违反：{expr}``(下一段)会**进用户屏幕** —— advisory 附注
            #   (``output.py::_validator_notice``)与阻塞档的 ``error_feedback``;
            # - ``判不了（…）``**不上屏**(``verdict is None`` 被 output.py 与
            #   validate.py 双双排除),它去的是 ``validator_hits`` —— 运行日志、
            #   会话详情、日后的质检统计,读它的是**看同一段对话的管理员**。
            # 两处的读者都跟着这次对话的语言,所以都本地化。上面几条(mode /
            # severity / host / 畸形 checks)保持英文:它们逐字引用配置键名,
            # 更接近错误码,处置在读的那个文件里而不在这句话里。
            if got is UNKNOWN:
                verdict = None
                reason = _unknown_reason(check, columns, rows, row_count)
                text = _UNKNOWN_TEXTS[reason]
                message = fallback or L(lang, text[0], text[1])
                break
            if got is not True:
                verdict = False
                message = fallback or L(
                    lang, f"违反：{expr}", f"violated: {expr}"
                )
                break
        hit: dict[str, Any] = {
            "name": name,
            "verdict": verdict,
            "severity": severity,
            "message": message,
            "mode": "deterministic",
        }
        if verdict is None:
            # 判不了才带原因码:判定过的 hit 没有"为什么"
            hit["reason"] = reason or "unknown_value"
        out.append(hit)
    return out
