"""Decision-condition expression language — safe by construction.

Rule conditions are authored by admins and live in a YAML file that anyone
with write access to the KB can edit. They are therefore **untrusted input**
and are never passed to ``eval()``: this module is a hand-written tokenizer
plus recursive-descent parser over a deliberately tiny closed grammar, whose
evaluator can only ever read from a caller-supplied scope dict.

    expr    := or
    or      := and ('or' and)*
    and     := not ('and' not)*
    not     := 'not' not | comparison
    comparison := operand (OP operand)?
    operand := '-' operand | '+' operand
             | NUMBER | STRING | IDENT
             | func '(' operand (',' operand)* ')'
             | '(' or ')'
    OP      := > >= < <= == !=

Two deliberate departures from the older ``jobs/alerts.py`` DSL:

**Closed scope, checked at parse time.** The identifier set is fixed
(``VARIABLES``) and the function table is fixed (``FUNCTIONS``). A typo'd
identifier — or an unregistered function such as the reserved ``trend`` —
is a *parse* error, surfaced by lint, rather than a silently-unknown value
that makes a rule quietly never fire.

**Kleene three-valued logic.** ``jobs/alerts.py`` established the contract
"unknown → False, never raise", which is right for a bare comparison but
wrong the moment ``not`` enters the language: with two-valued logic a
missing baseline makes ``not (x > y)`` evaluate to True and the rule fires
on absent data. So operands resolve to True / False / **Unknown**, ``not
Unknown`` is ``Unknown``, and an Unknown top level never triggers.

``Unknown`` is a singleton whose ``__bool__`` raises: an accidental
``if value:`` is a loud bug, not a silent third-value collapse.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


class DecisionExprError(ValueError):
    """Malformed or undeclared condition expression — surfaced by lint."""


class _Unknown:
    """Kleene's third truth value: "cannot be determined from the data"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Unknown"

    def __bool__(self) -> bool:
        raise TypeError(
            "Unknown has no truth value — compare with `is UNKNOWN` instead")


UNKNOWN = _Unknown()

#: Every identifier a condition may reference. Closed on purpose: the parser
#: rejects anything else, so a misspelling cannot become a silent no-op.
VARIABLES: frozenset[str] = frozenset({
    "current",        # metric value in the rule's window
    "baseline",       # metric value in the baseline window (Unknown if none)
    "delta",          # current - baseline
    "delta_pct",      # (current - baseline) / baseline
    "contribution",   # signed share of Σ|delta| across dimensions
    "row_count",      # rows returned by the current-window query
    "dim",            # dimension label of the row being evaluated
})

#: validator 档的标识符闭集 —— **结果域**,与上面的决策域是两套词。
#:
#: 同样是闭集,同样在**写入时**就拒掉拼错的变量名(见 SkillService 的
#: ``_validate_validator_spec``):开放变量集下,``mn >= 0`` 会变成一条永远
#: 求值为 Unknown 的静默 no-op,而 no-op 从外面看和"检查通过"一模一样。
#:
#: ``min`` / ``max`` 与 ``FUNCTIONS`` 里的同名函数**共存**是合法的:消歧靠
#: 语法(后面跟 ``(`` 是调用,否则是标识符),不需要改名。
VALIDATOR_VARIABLES: frozenset[str] = frozenset({
    "min",         # 点名各列合并后的最小值(任一列为负即触发)
    "max",         # 合并后的最大值
    "sum",         # 合并后的求和
    "avg",         # 合并后的均值
    "null_count",  # 点名各列的 NULL(含行短于列数)计数
    "row_count",   # 结果行数
    "col_count",   # 结果列数
})

#: name → (min arity, max arity or None for variadic). A name absent here is
#: a parse error, which is how ``trend(n)`` stays reserved rather than half-built.
FUNCTIONS: dict[str, tuple[int, int | None]] = {
    "abs": (1, 1),
    "min": (2, None),
    "max": (2, None),
    "pct_change": (2, 2),   # pct_change(new, old)
}

_KEYWORDS = frozenset({"and", "or", "not"})

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<str>"[^"]*"|'[^']*')
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>>=|<=|==|!=|>|<)
    | (?P<sign>[-+])
    | (?P<punct>[(),])
    """,
    re.VERBOSE,
)

_COMPARISONS = frozenset({">", ">=", "<", "<=", "==", "!="})


# ── value coercion ───────────────────────────────────────────

def as_number(value: Any) -> float | None:
    """Best-effort numeric coercion; **None** (not 0.0) when not a number.

    ``attribution._num`` collapses unparseable input to 0.0, which is fine
    for display percentages. Here it would be a silent wrong answer — 0.0
    is a confident "definitely not triggered", whereas None becomes Unknown
    and correctly declines to judge.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        f = float(value)
    else:
        s = str(value).strip()
        if s.endswith("%"):
            s = s[:-1].strip()
        s = s.replace(",", "").replace("，", "").lstrip("¥$€£￥")
        try:
            f = float(s)
        except ValueError:
            return None
    return f if math.isfinite(f) else None


# ── AST ──────────────────────────────────────────────────────

class Node:
    __slots__ = ()

    def eval(self, scope: dict[str, Any]) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def identifiers(self) -> set[str]:
        return set()


@dataclass(frozen=True)
class Num(Node):
    value: float

    def eval(self, scope):
        return self.value


@dataclass(frozen=True)
class Str(Node):
    value: str

    def eval(self, scope):
        return self.value


@dataclass(frozen=True)
class Ident(Node):
    name: str

    def eval(self, scope):
        raw = scope.get(self.name, None)
        n = as_number(raw)
        if n is not None:
            return n
        # Not a number — keep a real label (`dim`) as a string so string
        # equality works; anything else (None, "", "N/A") is Unknown.
        if isinstance(raw, str) and raw.strip():
            return raw
        return UNKNOWN

    def identifiers(self) -> set[str]:
        return {self.name}


@dataclass(frozen=True)
class Neg(Node):
    operand: Node

    def eval(self, scope):
        v = self.operand.eval(scope)
        if v is UNKNOWN or isinstance(v, str):
            return UNKNOWN
        return -v

    def identifiers(self) -> set[str]:
        return self.operand.identifiers()


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple[Node, ...]

    def eval(self, scope):
        vals = [a.eval(scope) for a in self.args]
        if any(v is UNKNOWN for v in vals):
            return UNKNOWN
        if any(isinstance(v, str) for v in vals):
            return UNKNOWN
        if self.name == "abs":
            return abs(vals[0])
        if self.name == "min":
            return min(vals)
        if self.name == "max":
            return max(vals)
        if self.name == "pct_change":     # pct_change(new, old)
            new, old = vals
            if old == 0:
                return UNKNOWN          # matches the compiler's NULLIF(...,0)
            return (new - old) / old
        return UNKNOWN

    def identifiers(self) -> set[str]:
        out: set[str] = set()
        for a in self.args:
            out |= a.identifiers()
        return out


@dataclass(frozen=True)
class Cmp(Node):
    op: str
    left: Node
    right: Node

    def eval(self, scope):
        left = self.left.eval(scope)
        right = self.right.eval(scope)
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        left_is_str = isinstance(left, str)
        right_is_str = isinstance(right, str)
        if left_is_str != right_is_str:
            # Cross-type comparison is not "False" — under `not` that would
            # flip into a false positive. Decline to judge.
            return UNKNOWN
        try:
            if self.op == "==":
                return left == right
            if self.op == "!=":
                return left != right
            if self.op == ">":
                return left > right
            if self.op == ">=":
                return left >= right
            if self.op == "<":
                return left < right
            if self.op == "<=":
                return left <= right
        except TypeError:
            return UNKNOWN
        return UNKNOWN

    def identifiers(self) -> set[str]:
        return self.left.identifiers() | self.right.identifiers()


@dataclass(frozen=True)
class Not(Node):
    operand: Node

    def eval(self, scope):
        v = self.operand.eval(scope)
        if v is UNKNOWN:
            return UNKNOWN
        return not v

    def identifiers(self) -> set[str]:
        return self.operand.identifiers()


@dataclass(frozen=True)
class And(Node):
    items: tuple[Node, ...]

    def eval(self, scope):
        results = [i.eval(scope) for i in self.items]
        if any(r is False for r in results):
            return False
        if all(r is True for r in results):
            return True
        return UNKNOWN

    def identifiers(self) -> set[str]:
        out: set[str] = set()
        for i in self.items:
            out |= i.identifiers()
        return out


@dataclass(frozen=True)
class Or(Node):
    items: tuple[Node, ...]

    def eval(self, scope):
        results = [i.eval(scope) for i in self.items]
        if any(r is True for r in results):
            return True
        if all(r is False for r in results):
            return False
        return UNKNOWN

    def identifiers(self) -> set[str]:
        out: set[str] = set()
        for i in self.items:
            out |= i.identifiers()
        return out


# ── tokenizer ────────────────────────────────────────────────

@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str
    pos: int


def _tokenize(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i = 0
    n = len(text)
    while i < n:
        m = _TOKEN_RE.match(text, i)
        if m is None:
            raise DecisionExprError(
                f"unexpected character {text[i]!r} at position {i}")
        i = m.end()
        kind = m.lastgroup or ""
        if kind == "ws":
            continue
        value = m.group()
        if kind == "ident" and value in _KEYWORDS:
            kind = "kw"
        toks.append(_Tok(kind, value, m.start()))
    toks.append(_Tok("eof", "", n))
    return toks


# ── parser ───────────────────────────────────────────────────

class _Parser:
    def __init__(self, toks: list[_Tok], text: str,
                 variables: frozenset[str] = VARIABLES):
        self.toks = toks
        self.text = text
        self.variables = variables
        self.i = 0

    # -- token helpers --
    @property
    def cur(self) -> _Tok:
        return self.toks[self.i]

    def advance(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def at_op(self, *ops: str) -> bool:
        return self.cur.kind == "op" and self.cur.value in ops

    def at_kw(self, *kws: str) -> bool:
        return self.cur.kind == "kw" and self.cur.value in kws

    def at_punct(self, ch: str) -> bool:
        return self.cur.kind == "punct" and self.cur.value == ch

    def fail(self, msg: str) -> None:
        raise DecisionExprError(f"{msg} (at position {self.cur.pos} in {self.text!r})")

    # -- grammar --
    def parse(self) -> Node:
        node = self.parse_or()
        if self.cur.kind != "eof":
            self.fail(f"unexpected trailing {self.cur.value!r}")
        return node

    def parse_or(self) -> Node:
        items = [self.parse_and()]
        while self.at_kw("or"):
            self.advance()
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else Or(tuple(items))

    def parse_and(self) -> Node:
        items = [self.parse_not()]
        while self.at_kw("and"):
            self.advance()
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else And(tuple(items))

    def parse_not(self) -> Node:
        if self.at_kw("not"):
            self.advance()
            return Not(self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        left = self.parse_operand()
        if self.cur.kind == "op" and self.cur.value in _COMPARISONS:
            op = self.advance().value
            right = self.parse_operand()
            return Cmp(op, left, right)
        # A bare parenthesised sub-expression is itself boolean; anything else
        # (a naked number, identifier or call) is a half-written condition.
        if isinstance(left, (Cmp, Not, And, Or)):
            return left
        self.fail("expected a comparison operator")

    def parse_operand(self) -> Node:
        t = self.cur
        if t.kind == "sign" and t.value == "-":
            self.advance()
            return Neg(self.parse_operand())
        if t.kind == "sign" and t.value == "+":
            self.advance()
            return self.parse_operand()
        if t.kind == "num":
            self.advance()
            return Num(float(t.value))
        if t.kind == "str":
            self.advance()
            return Str(t.value[1:-1])
        if t.kind == "ident":
            self.advance()
            return self.parse_ident(t)
        if self.at_punct("("):
            self.advance()
            node = self.parse_or()
            if not self.at_punct(")"):
                self.fail("missing closing ')'")
            self.advance()
            return node
        self.fail(f"expected a value, got {t.value!r}" if t.kind != "eof"
                  else "expected a value, got end of expression")

    def parse_ident(self, t: _Tok) -> Node:
        name = t.value
        if self.at_punct("("):
            if name not in FUNCTIONS:
                known = ", ".join(sorted(FUNCTIONS))
                raise DecisionExprError(
                    f"unknown function {name!r} at position {t.pos} — "
                    f"available: {known}")
            lo, hi = FUNCTIONS[name]
            self.advance()  # consume '('
            args: list[Node] = [self.parse_operand()]
            while self.at_punct(","):
                self.advance()
                args.append(self.parse_operand())
            if not self.at_punct(")"):
                self.fail("missing closing ')' in call")
            self.advance()
            if len(args) < lo or (hi is not None and len(args) > hi):
                want = f"{lo}" if hi == lo else (
                    f"at least {lo}" if hi is None else f"{lo}–{hi}")
                raise DecisionExprError(
                    f"{name}() takes {want} argument(s), got {len(args)}")
            return Call(name, tuple(args))
        if name not in self.variables:
            known = ", ".join(sorted(self.variables))
            raise DecisionExprError(
                f"unknown identifier {name!r} at position {t.pos} — "
                f"available: {known}")
        return Ident(name)


# ── public API ───────────────────────────────────────────────

@functools.lru_cache(maxsize=512)
def parse_condition(text: str, variables: frozenset[str] = VARIABLES) -> Node:
    """Condition text → AST. Raises ``DecisionExprError`` when malformed.

    Cached: ASTs are immutable and rules are re-parsed on every scheduler
    tick. Parse errors are *not* cached — a rule being edited stays fixable.
    ``variables`` selects the identifier closed set (decision domain by
    default, ``VALIDATOR_VARIABLES`` for result-level checks); it is part of
    the cache key, so the two domains never share a cached AST.
    """
    text = (text or "").strip()
    if not text:
        raise DecisionExprError("condition must not be empty")
    return _Parser(_tokenize(text), text, variables).parse()


def condition_variables(text: str, variables: frozenset[str] = VARIABLES) -> set[str]:
    """Identifiers a condition reads — used by lint to flag, e.g., a
    ``contribution`` test on a rule that produces no dimensions."""
    return parse_condition(text, variables).identifiers()


def evaluate_condition(text: str, scope: dict[str, Any],
                       variables: frozenset[str] = VARIABLES) -> bool:
    """True only when the condition is *definitely* satisfied.

    Unknown (missing baseline, unparseable value, type mismatch) is False —
    the no-false-positive contract the alert DSL established.

    **validator 不要用这个函数**:它把 Unknown 塌成 False,而 validator 需要
    区分「判不了」与「违反」。走 ``parse_condition(...).eval(scope)`` 直接拿
    三值(见 ``trove/services/skills/validators.py``)。
    """
    return parse_condition(text, variables).eval(scope) is True
