"""Deterministic error classifier — one exit, machine-readable class per failure.

Failure surfaces are scattered across the pipeline (LLM gateway, agent-loop
tool dispatch, SQL executor, gen_sql probes, workflow diagnostics). Each
layer folded errors into plain text and retried blindly. ``classify_error``
gathers them into a single ``ErrorClass`` the recovery layer can act on
deterministically — no LLM, no per-layer if-else buckets:

  exception type / status code  →  deterministic lexicon  →  UNKNOWN

Flow: the class decides *retryability* and *recovery action*; only
``needs_analysis`` errors (semantics the lexicons cannot decide) should
reach the LLM diagnostic node. The classifier is dependency-light
(typing + re only) so gateways, loops, nodes and probes can import it
without import-cycle risk.

The ``[ERR:<id>]`` tag is the visible contract: every error folded into an
observation / error_feedback / trace first passes here and carries its
machine-readable class through the chain.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

# ── Recovery actions ──────────────────────────────────────
# 每个类只对应一个恢复动作,恢复逻辑按 ID 幂等分发。


class RecoveryAction(str, enum.Enum):
    FIX = "fix"                          # 定点修复当前产出(语法/类型错误)
    FIX_ARGS = "fix_args"                # 修正工具参数,不重跑该次调用
    ROLLBACK_SCHEMA = "rollback:schema_linking"
    ROLLBACK_PLANNER = "rollback:planner"
    ROLLBACK_INTENT = "rollback:route_intent"
    RETRY_BACKOFF = "retry:backoff"      # 指数退避后重试(默认上限=配置)
    RETRY_AFTER = "retry:after"          # 按服务端 retry_after 等待后重试
    RECONNECT = "retry:reconnect"        # 重建连接后重跑同一调用
    SIMPLE_REGENERATE = "fix:simple"     # 语法位置精确可知 → 定向微调,不整句重生成
    SIMPLIFY = "simplify"                # 降复杂度后重试(超时)
    SURFACE = "surface"                  # 直接报告用户,自动恢复无意义
    INTERNAL = "internal"                # python/实现级 bug,降级上报
    BUDGET = "budget"                    # 预算护栏,优雅降级
    NOOP = "noop"                        # 不是错误(空结果等)
    ANALYZE = "analyze"                  # 交给 LLM 诊断(UNKNOWN 兜底)


# ── Error classes ─────────────────────────────────────────


@dataclass(frozen=True)
class ErrorClass:
    """One machine-readable error class + its recovery contract.

    ``tag()`` renders the ``[ERR:<id>]`` prefix layered onto every error
    folded into observations / error feedback along the pipeline.
    """

    id: str
    domain: str                # llm | ds | sql | plan | runtime | budget | result
    severity: str              # fatal | error | warn | info
    retryable: bool            # 同一条调用重跑是否有意义
    recovery: RecoveryAction   # 恢复动作(恢复层按此分发)
    needs_analysis: bool = True   # True → 仍应由 analyze_error 的 LLM 判
    retry_after: bool = False     # 服务端给了 retry-after 信号(重试层读取)
    user_msg: str = ""

    def tag(self) -> str:
        return f"[ERR:{self.id}]"


# 注册表:顺序即默认展示顺序(severity 由各自字段决定)
CLASSES: dict[str, ErrorClass] = {
    c.id: c
    for c in [
        # ── LLM 后端 ──────────────────────────────────────
        ErrorClass(
            "LLM_TRANSIENT", "llm", "error", retryable=True,
            recovery=RecoveryAction.RETRY_BACKOFF, needs_analysis=False,
            retry_after=True,
            user_msg="LLM provider temporarily unavailable, retrying.",
        ),
        ErrorClass(
            "LLM_SERVICE", "llm", "fatal", retryable=False,
            recovery=RecoveryAction.SURFACE, needs_analysis=False,
            user_msg="LLM provider rejected the request (auth/model/context).",
        ),
        # ── 数据源 ────────────────────────────────────────
        ErrorClass(
            "DS_TRANSIENT", "ds", "warn", retryable=True,
            recovery=RecoveryAction.RECONNECT, needs_analysis=False,
            user_msg="Datasource connection hiccup, reconnecting.",
        ),
        ErrorClass(
            "RATE_LIMIT", "ds", "error", retryable=True,
            recovery=RecoveryAction.RETRY_AFTER, needs_analysis=False,
            retry_after=True,
            user_msg="Datasource is throttling; waiting before retry.",
        ),
        ErrorClass(
            "DS_AUTH", "ds", "fatal", retryable=False,
            recovery=RecoveryAction.SURFACE, needs_analysis=False,
            user_msg="Datasource denied access (credentials/permission).",
        ),
        # ── SQL 静态 ──────────────────────────────────────
        ErrorClass(
            "SQL_SYNTAX", "sql", "error", retryable=True,
            # 语法错误位置是确定性信息(SQLGlot 报 Line/Col/token)——定向微调
            # 该位置即可,不需要整句重生成(不烧完整 revisor/修复轮)。
            recovery=RecoveryAction.SIMPLE_REGENERATE,
            user_msg="SQL syntax error; fix the offending token locally.",
        ),
        ErrorClass(
            "SQL_SCHEMA_MISSING", "sql", "error", retryable=True,
            recovery=RecoveryAction.ROLLBACK_SCHEMA,
            user_msg="Referenced table/column does not exist; re-link schema.",
        ),
        ErrorClass(
            "SQL_EXEC_TYPE", "sql", "error", retryable=True,
            recovery=RecoveryAction.FIX,
            user_msg="SQL type/aggregation mismatch; fix the expression.",
        ),
        ErrorClass(
            "SQL_PERMISSION", "sql", "fatal", retryable=False,
            recovery=RecoveryAction.SURFACE, needs_analysis=False,
            user_msg="Operation not permitted under the current permission level.",
        ),
        # ── SQL 结果 ──────────────────────────────────────
        ErrorClass(
            "SQL_TIMEOUT", "sql", "error", retryable=True,
            recovery=RecoveryAction.SIMPLIFY,
            user_msg="Query timed out; simplify the SQL and retry.",
        ),
        ErrorClass(
            "SQL_EMPTY", "result", "info", retryable=False,
            recovery=RecoveryAction.NOOP,
            user_msg="Query returned zero rows.",
        ),
        ErrorClass(
            "RESULT_NULLDUP", "result", "warn", retryable=False,
            recovery=RecoveryAction.NOOP,
            user_msg="Result contains unexpected nulls/duplicates.",
        ),
        # ── 规划/意图 ─────────────────────────────────────
        ErrorClass(
            "PLAN_DRIFT", "plan", "error", retryable=True,
            recovery=RecoveryAction.ROLLBACK_PLANNER,
            user_msg="Generated columns drifted from the plan.",
        ),
        ErrorClass(
            "INTENT_MISROUTE", "plan", "error", retryable=True,
            recovery=RecoveryAction.ROLLBACK_INTENT,
            user_msg="Intent was misrouted; re-route the question.",
        ),
        # ── 运行时/tool ───────────────────────────────────
        ErrorClass(
            "ARGS_SCHEMA", "runtime", "error", retryable=False,
            recovery=RecoveryAction.FIX_ARGS, needs_analysis=False,
            user_msg="Tool call arguments failed validation.",
        ),
        ErrorClass(
            "TOOL_TIMEOUT", "runtime", "error", retryable=True,
            recovery=RecoveryAction.RETRY_BACKOFF, needs_analysis=False,
            user_msg="Tool call timed out.",
        ),
        ErrorClass(
            "TOOL_RUNTIME", "runtime", "fatal", retryable=False,
            recovery=RecoveryAction.INTERNAL, needs_analysis=False,
            user_msg="Internal tool execution error.",
        ),
        ErrorClass(
            "BUDGET", "budget", "warn", retryable=False,
            recovery=RecoveryAction.BUDGET, needs_analysis=False,
            user_msg="Agent budget exhausted; degrading gracefully.",
        ),
        ErrorClass(
            "INTERRUPT", "runtime", "warn", retryable=False,
            recovery=RecoveryAction.NOOP, needs_analysis=False,
            user_msg="Operation was interrupted.",
        ),
        # ── 兜底 ──────────────────────────────────────────
        ErrorClass(
            "UNKNOWN", "runtime", "warn", retryable=True,
            recovery=RecoveryAction.ANALYZE,
            user_msg="Unknown failure; diagnosing.",
        ),
    ]
}

# analyze_error 的确定性短路集合:这些类 LLM 诊断是纯浪费(语义不可修),
# 直接带 user_msg 降级/上报,不烧诊断 token。
DETERMINISTIC_DEAD_END = {
    "DS_AUTH", "SQL_PERMISSION", "TOOL_RUNTIME", "ARGS_SCHEMA",
    "LLM_SERVICE", "INTERRUPT",
}


# ── Deterministic lexicon (context-gated, ordered) ────────

# context 取值: "llm" | "sql" | "tool" | "workflow" | "" (不限制)
_LLM_CTX = frozenset({"llm"})
_SQL_CTX = frozenset({"sql", "workflow", ""})
_TOOL_CTX = frozenset({"tool", ""})
_ANY = frozenset({""})

# (pattern, class_id, allowed_contexts|None)
_LEXICON: list[tuple[re.Pattern, str, frozenset[str] | None]] = []
_RULES: list[tuple[str, str, frozenset[str] | None]] = [
    # ── LLM 层(仅 LLM 上下文,避免 SQL 文本里数字误伤) ─────
    (r"rate.?\s?limit|too many requests|quota exceeded|throttl|status.?code.?429|\b429\b",
     "LLM_TRANSIENT", _LLM_CTX),
    (r"\b5\d\d\b|internal server error|bad gateway|gateway timeout|service unavailable|overloaded|temporar.?ly unavailable|upstream",
     "LLM_TRANSIENT", _LLM_CTX),
    (r"connection (?:reset|refused|closed|aborted|timed out)|broken pipe|api.?timeout|timed out|timeout|\bAPITimeoutError\b|\bAPIConnectionError\b",
     "LLM_TRANSIENT", _LLM_CTX),
    (r"\b401\b|\b403\b|\b404\b|\bunauthorized\b|\binvalid (?:api.?key|credential)|api.?key|model not found|unknown model|authentication",
     "LLM_SERVICE", _LLM_CTX),
    (r"context(?:ual)?\s*(?:length|window|limit)|maximum context|input (?:tokens|length) (?:exceed|too large)|context length exceeded",
     "LLM_SERVICE", _LLM_CTX),

    # ── 数据源瞬态(文本即可判;无上下文限制) ────────────────
    (r"server has gone away|lost connection|connection (?:reset|refused|closed|terminated|timed out)|closed connection|cannot connect|could not connect|unreachable|broken pipe|handshake|stale|not connected|eof|network partition|database is locked|deadlock|mysql server has gone away",
     "DS_TRANSIENT", None),
    (r"too many connections|too many clients|max.?connections|too many requests",
     "RATE_LIMIT", None),
    (r"access denied|authentication failed|password.*(?:incorrect|failed)|invalid (?:username|user|password|login)|permission denied to|command denied|denied by row level|row.?level security|\brls\b",
     "DS_AUTH", None),

    # ── SQL 权限 / guard ──────────────────────────────────
    (r"not permitted|not allowed|write operations|read.?only|forbidden|disallow(?:ed)?|unsafe",
     "SQL_PERMISSION", _SQL_CTX),

    # ── SQL 缺 schema(表/列/库;does not exist 限 schema 实体词,避免
    #    "operator does not exist" 误判) ───────────────────
    (r"no such (?:table|column|database)|unknown (?:column|table|database)|(?:table|relation|column|database|schema)\b(?:\s+\S+){0,6}\s+does not exist|table\s+['\"][^'\"]+['\"]\s+not found|column\s+['\"][^'\"]+['\"]\s+not found|not found in\s+['\"][^'\"]+['\"]|\bno attribute\b",
     "SQL_SCHEMA_MISSING", _SQL_CTX),

    # ── SQL 语法 ──────────────────────────────────────────
    (r"syntax(?: error)?|in your sql syntax|\b1064\b|unrecognized token|expected token|unexpected token|error parsing|parse error|invalid expression|missing (?:clause|keyword|from|where|group)|near\s+['\"][^'\"]|sqlglot|must be a single statement|not a valid (?:select|sql)",
     "SQL_SYNTAX", _SQL_CTX),

    # ── SQL 类型/聚合 ─────────────────────────────────────
    (r"type mismatch|cannot cast|invalid input syntax|cast.*?fail|operator (?:does not exist|is not unique)|no such function|no function matches|wrong number of arguments|ambiguous column|out of range|numeric value out of range|incorrect (?:double|integer|decimal|value)|misuse of aggregate|not a group by expression|must appear in the group by|no rows emitted|non.?numeric|not numeric|division by zero|no operator matches|invalid (?:utf-8|byte sequence)|truncate.*incorrect",
     "SQL_EXEC_TYPE", _SQL_CTX),

    # ── 超时(EXEC_CTX;LLM 超时走上面 LLM 层) ──────────────
    (r"timed out|timeout|query was canceled|canceled by user|wait timeout exceeds",
     "SQL_TIMEOUT", _SQL_CTX),

    # ── 空结果(不是错误,但需要专门裁决) ───────────────────
    (r"(?:no|zero|0)\s+rows|empty result|zero results|结果为空|零行",
     "SQL_EMPTY", _SQL_CTX),

    # ── python 实现级 bug(工具/节点代码缺陷) ─────────────
    (r"Traceback|AssertionError|assert(?:ion)? failed|KeyError|AttributeError|TypeError|ValueError|ImportError|NotImplementedError|ZeroDivisionError|Internal Server Error",
     "TOOL_RUNTIME", _TOOL_CTX),

    # ── 工具调用契约 ──────────────────────────────────────
    (r"missing required field|invalid arguments|unknown tool",
     "ARGS_SCHEMA", _TOOL_CTX),
]

for _pat, _cls, _ctx in _RULES:
    _LEXICON.append((re.compile(_pat, re.I), _cls, _ctx))

# 异常类型名后缀 → ds 瞬态(不依赖文本;覆盖各驱动命名差异)
_DS_TRANSIENT_TYPE = (
    "OperationalError", "InterfaceError", "ConnectionError", "LostConnection",
    "TransportError", "ProxyError", "ConnectionRefusedError",
    "ConnectionResetError", "ConnectionAbortedError", "AbandonConnection",
)

# 异常类型名 → python 实现级 bug(重试无意义)。不含 RuntimeError:它是
# LLM/网关层最常见的"通用承载"异常(如带 429/401 文本),文本词典优先。
_PYTHON_BUG_TYPE = (
    "ValueError", "TypeError", "KeyError", "AttributeError",
    "AssertionError", "ImportError", "NotImplementedError", "ZeroDivisionError",
)


# ── classify_error ────────────────────────────────────────


@dataclass
class ClassifiedError:
    """Classification verdict: the class plus the evidence that matched.

    ``tag()`` == ``cls.tag()``; ``retryable``/``recovery``/``needs_analysis``
    proxy through to the class for ergonomics.
    """

    cls: ErrorClass
    signals: list[str] = field(default_factory=list)

    def tag(self) -> str:
        return self.cls.tag()

    @property
    def retryable(self) -> bool:
        return self.cls.retryable

    @property
    def recovery(self) -> RecoveryAction:
        return self.cls.recovery

    @property
    def needs_analysis(self) -> bool:
        return self.cls.needs_analysis

    def describe(self) -> str:
        if self.cls.id == "UNKNOWN":
            return "unknown"
        return f"{self.tag()} {self.cls.domain}/{self.cls.recovery.value}"


def _status_class(exc: BaseException, context: str) -> ErrorClass | None:
    """强类型信号:HTTP 状态码 + 内建异常类型(优先于文本词典)。"""
    if exc is None:
        return None
    if isinstance(exc, (KeyboardInterrupt,)):
        return CLASSES["INTERRUPT"]
    import asyncio as _asyncio

    if isinstance(exc, (_asyncio.CancelledError,)):
        return CLASSES["INTERRUPT"]
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if context == "llm":
            if status == 429 or status >= 500:
                return CLASSES["LLM_TRANSIENT"]
            if status in (401, 403, 404):
                return CLASSES["LLM_SERVICE"]
    if isinstance(exc, TimeoutError):
        if context == "llm":
            return CLASSES["LLM_TRANSIENT"]
        if context == "tool":
            return CLASSES["TOOL_TIMEOUT"]
        return CLASSES["SQL_TIMEOUT"]
    name = type(exc).__name__
    if name.endswith(_DS_TRANSIENT_TYPE):
        return CLASSES["LLM_TRANSIENT"] if context == "llm" else CLASSES["DS_TRANSIENT"]
    if name in _PYTHON_BUG_TYPE:
        return CLASSES["TOOL_RUNTIME"]
    return None


def classify_error(
    text: str = "",
    *,
    exc: BaseException | None = None,
    context: str = "workflow",
) -> ClassifiedError:
    """Fold (exception type + text) into one ErrorClass.

    Third-level fallback is ``UNKNOWN`` (retryable=True, ANALYZE) so the
    LLM diagnostic keeps responsibility for anything the lexicons cannot
    decide — it never silently swallows.

    Args:
        text: 错误文本(引擎报错/sanitize 后的异常信息,可能为空)。
        exc: 原始异常(强类型信号:HTTP 状态码/内建异常/驱动类型名)。
        context: 故障面——"llm" | "sql" | "tool" | "workflow";决定
            LLM/SQL 专属词典的生效范围,避免跨层误分类。
    """
    strong = _status_class(exc, context)
    if strong is not None and strong.id != "UNKNOWN":
        return ClassifiedError(strong, signals=[type(exc).__name__])

    low = (text or "").lower()
    if low:
        for pattern, cls_id, allowed in _LEXICON:
            if allowed is not None and context not in allowed:
                continue
            if pattern.search(low):
                return ClassifiedError(
                    CLASSES[cls_id], signals=[cls_id, pattern.pattern[:60]],
                )

    # 无文本、无强信号:类名兜底后再 UNKNOWN
    if exc is not None:
        name = type(exc).__name__
        if name.endswith(_DS_TRANSIENT_TYPE):
            return ClassifiedError(
                CLASSES["LLM_TRANSIENT"] if context == "llm" else CLASSES["DS_TRANSIENT"],
                signals=[name],
            )
    return ClassifiedError(CLASSES["UNKNOWN"], signals=[])


def tag_error(text: str, *, context: str = "workflow") -> str:
    """Fold text and prefix with ``[ERR:<id>]`` — nothing to tag → unchanged.

    The canonical way for probe/node layers to make error classes visible
    in observations / error_feedback:
        "no such table: loans" → "[ERR:SQL_SCHEMA_MISSING] no such table: loans"
    """
    verdict = classify_error(text, context=context)
    if verdict.cls.id == "UNKNOWN":
        return text
    if text.startswith(verdict.tag()):  # 幂等:已打过标不重复
        return text
    return f"{verdict.tag()} {text}"


def is_transient(exc: BaseException) -> bool:
    """同一条调用重跑是否有意义的判定(连接层);execute_sql 复用。

    Kept as the single source for "retry this SQL call" decisions: only
    connection/handshake-class failures qualify — syntax, schema-missing,
    permission all return False (retry would be pointless).
    """
    return classify_error(str(exc), exc=exc, context="sql").cls.id in {
        "DS_TRANSIENT", "RATE_LIMIT",
    }


# ── Lightweight tool-argument validation ──────────────────
# JSON-schema 浅校验(无 jsonschema 依赖):required + 顶层类型 + enum。
# 目的不是完整校验,是拦截"模型拼错参数"这一最高频的预算浪费——
# 参数错重跑工具必死,应在分派前一道防火墙拦下。


_SCALAR_TYPES = {
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "string": (str,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(
    parameters: dict[str, Any] | None,
    args: dict[str, Any] | None,
) -> list[str]:
    """Shallow JSON-schema validation of tool arguments.

    Returns:
        问题列表;空列表 = 通过。None/未声明的参数跳过。
    """
    params = parameters or {}
    properties = params.get("properties") or {}
    required = params.get("required") or []
    args = args or {}
    problems: list[str] = []
    for name in required:
        if name not in args:
            problems.append(f"missing required field '{name}'")
    for name, spec in properties.items():
        if name not in args or args.get(name) is None:
            continue
        value = args[name]
        type_name = spec.get("type")
        if type_name in _SCALAR_TYPES:
            ok = isinstance(value, _SCALAR_TYPES[type_name])
            if type_name == "number":
                ok = ok and not isinstance(value, bool)
            if type_name == "integer":
                ok = ok and not isinstance(value, bool) and not isinstance(value, float)
            if not ok:
                problems.append(
                    f"field '{name}' must be of type {type_name}, got {type(value).__name__}"
                )
        enum_values = spec.get("enum")
        if enum_values is not None and value not in enum_values:
            problems.append(f"field '{name}' must be one of {enum_values}")
    return problems