"""Closed-set rendering of an action payload — safe by construction.

A template is a JSON document with ``{{variable}}`` holes; the holes may only
name variables from :data:`TEMPLATE_VARIABLES`, a closed set the decision
layer can actually supply. There is no expression language, no Jinja, no
``eval`` — the same discipline as ``decision/expr.py``, for the same reason:
the thing being templated ends up in **an outbound message**, and an outbound
message that silently rendered an empty value is the worst outcome this
module can produce.

Three deliberate choices:

**Unknown variable = hard error.** ``{{mesage}}`` (typo) must not render as
``""`` — a webhook payload with a silently-missing field looks exactly like a
deliberately-empty one, and nobody finds out until the message is already in
the wrong inbox. The allowed names are in the error text.

**A referenced variable with no value = hard error.** The set of variables a
*rule* can supply is narrower than the closed set (``dim`` means nothing for
an aggregate rule with no baseline). A template that asks for something the
rule cannot give is a mismatch between two admin-authored assets, and the
honest moment to find it is proposal-creation time — the proposal is then not
created at all (「宁可不发不发半成品」), never created half-empty.

**Values are JSON-escaped before substitution.** A message containing ``"``
or a newline (or an injection-shaped string) cannot break out of the string
literal it was placed in: the substitution writes
``json.dumps(value)[1:-1]``, so the result parses back to exactly the value
that went in. The rendered text is then parsed with ``json.loads`` — a
template that is not valid JSON, or that does not parse to an object, is
refused by *parsing*, not by a promise.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: The closed set of variables an action template may interpolate.
#: Every name here has a defined source at proposal-creation time
#: (rule / verdict / proposal identity) — see ``propose.build_variables``.
TEMPLATE_VARIABLES = frozenset({
    # rule identity (from the decision rule the verdict was judged under)
    "rule_id", "rule_name", "rule_digest",
    # verdict
    "severity", "priority", "message", "recommendation",
    # datasource + metric + the primary group the rule fired on
    "datasource", "metric", "dim",
    "current", "baseline", "delta", "delta_pct",
    # time
    "anchor_date", "evaluated_at",
    # proposal identity (filled by the action layer itself)
    "proposal_id",
    # run identity
    "run_id", "job_id",
})

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

#: Sample values for every closed-set variable — used to *validate* a template
#: at create time (render it once against this) so an unknown name or a JSON
#: typo is a 400 to the admin who wrote it, not a surprise at first fire.
SAMPLE_VARIABLES: dict[str, str] = {
    "rule_id": "revenue-drop",
    "rule_name": "Revenue drop",
    "rule_digest": "0123456789abcdef",
    "severity": "warning",
    "priority": "2",
    "message": "[warning] Revenue drop — 当期 1,234, 变化 -12.3%",
    "recommendation": "Check the campaign calendar",
    "datasource": "financial",
    "metric": "revenue",
    "dim": "north",
    "current": "1234",
    "baseline": "1400",
    "delta": "-166",
    "delta_pct": "-0.1186",
    "anchor_date": "2026-10-03",
    "evaluated_at": "2026-10-03T09:00:00",
    "proposal_id": "p-0123456789abcdef",
    "run_id": "42",
    "job_id": "job-1",
}


class RenderError(ValueError):
    """A template that must not be rendered (or a proposal that must not exist)."""


def template_variables(text: str) -> list[str]:
    """Names referenced by ``text`` (declaration order, deduped). Raises on a
    malformed placeholder — ``{{Rule Id}}`` is a typo, not a variable."""
    out: list[str] = []
    for name, _ in _scan(text):
        if name not in out:
            out.append(name)
    return out


def _scan(text: str) -> list[tuple[str, tuple[int, int]]]:
    """``[(name, span)]`` for every ``{{name}}``; malformed → ``RenderError``."""
    text = text or ""
    found: list[tuple[str, tuple[int, int]]] = []
    i = 0
    while True:
        start = text.find("{{", i)
        if start < 0:
            break
        end = text.find("}}", start + 2)
        if end < 0:
            raise RenderError(
                f"unclosed '{{{{' at offset {start} — every placeholder must "
                "be closed with '}}'")
        name = text[start + 2:end].strip()
        if not _NAME_RE.match(name):
            raise RenderError(
                f"invalid placeholder {{{{{name}}}}} at offset {start} — "
                "variable names are lowercase letters, digits and underscores")
        found.append((name, (start, end + 2)))
        i = end + 2
    # A stray '}}' (opening brace typo'd away) would otherwise sit in the
    # output as literal text — valid JSON only by accident, and never what the
    # author meant.
    opens = text.count("{{")
    closes = text.count("}}")
    if opens != closes:
        raise RenderError(
            f"unbalanced placeholders: {opens} '{{{{' vs {closes} '}}}}'")
    return found


def _json_inner(value: Any) -> str:
    """Value → JSON string *contents* (escaped, no surrounding quotes).

    Placeholders sit inside a JSON string in every sane template
    (``"metric": "{{metric}}"``); writing the escaped inner form means (a) a
    value containing ``"`` or a newline cannot escape its literal, and (b) an
    unquoted numeric placeholder (``"priority": {{priority}}``) still renders
    as a number when the value looks like one.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    return json.dumps(str(text), ensure_ascii=False)[1:-1]


def render_payload(
    template_text: str, variables: Mapping[str, Any], *,
    max_bytes: int = 0,
) -> dict[str, Any]:
    """``template_text`` + ``variables`` → the payload dict to send.

    Raises ``RenderError`` for: a malformed or unknown placeholder, a closed-set
    variable with no value in ``variables``, output that is not valid JSON, or
    output that is not a JSON object. ``max_bytes`` (>0) bounds the rendered
    size — an outbound payload has no business being unbounded.
    """
    text = (template_text or "").strip()
    if not text:
        raise RenderError("payload_template is empty")
    chunks: list[str] = []
    cursor = 0
    for name, (start, end) in _scan(text):
        if name not in TEMPLATE_VARIABLES:
            allowed = ", ".join(sorted(TEMPLATE_VARIABLES))
            raise RenderError(
                f"unknown template variable {name!r} — allowed variables: {allowed}")
        if name not in variables:
            raise RenderError(
                f"template uses {{{{{name}}}}} but this rule cannot supply a "
                f"value for it (available here: "
                f"{', '.join(sorted(variables)) or 'none'})")
        chunks.append(text[cursor:start])
        chunks.append(_json_inner(variables[name]))
        cursor = end
    chunks.append(text[cursor:])
    rendered = "".join(chunks)
    if max_bytes and len(rendered.encode("utf-8")) > max_bytes:
        raise RenderError(
            f"rendered payload is {len(rendered.encode('utf-8'))} bytes, over "
            f"the configured limit of {max_bytes}")
    try:
        payload = json.loads(rendered)
    except ValueError as e:
        raise RenderError(f"rendered payload is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise RenderError(
            f"rendered payload must be a JSON object, got {type(payload).__name__}")
    return payload


def validate_template(template_text: str, *, max_bytes: int = 0) -> list[str]:
    """Create-time validation: render against :data:`SAMPLE_VARIABLES`.

    Returns a list of human-readable problems (empty = the template is
    renderable). Every closed-set variable is present in the sample, so this
    checks the *template*: unknown names, unbalanced placeholders, output that
    is not a JSON object. Whether the rule can supply the values a template
    asks for is a per-rule question, answered at proposal time.
    """
    try:
        render_payload(template_text, SAMPLE_VARIABLES, max_bytes=max_bytes)
    except RenderError as e:
        return [str(e)]
    return []
