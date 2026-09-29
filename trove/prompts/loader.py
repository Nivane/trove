"""Jinja2 prompt template loader.

Prompts live in trove/prompts/<node>/<name>.<lang>.j2 (lang = en | zh).
`render` picks the template by language and falls back to English when a
language-specific file does not exist — single-language prompts ship only
``.en.j2``.

Templates are rendered with the default (lenient) Undefined: a missing
variable renders as an empty string instead of raising, matching the old
``dict.get(key, "")`` call sites.

**不可信数据边界(A 通道)**:这是提示词的唯一渲染入口,插值前逐参数过隔离核
(``trove.llm.untrusted``)—— 命中即整值替换成中性标记,白名单参数放行,
模板自身的字面量不扫(作者可控)。
"""

from __future__ import annotations

import re

import jinja2

from trove.core.logging import get_logger
from trove.core.metrics import record_prompt_isolation
from trove.llm.untrusted import isolate_tree, is_trusted_var

logger = get_logger(__name__)

# name = "<node>/<name>": letters, digits, underscore, dash, single slash.
# Blocks path traversal through the template name.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+(/[A-Za-z0-9_-]+)*$")

_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("trove", "prompts"),
    autoescape=False,  # prompt text is plain text — never HTML-escape
)


def render(name: str, lang: str = "en", **vars: object) -> str:
    """Render a prompt template for the given language.

    Args:
        name: Template name without extension, e.g. "gen_sql/system".
        lang: Language code ("en" or "zh"); falls back to English when the
            language-specific file does not exist.
        vars: Template variables.

    Returns:
        Rendered prompt text.

    Raises:
        ValueError: Invalid template name, or template not found for any
            of the tried languages.
    """
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid prompt template name: {name!r}")

    for candidate in (lang, "en"):
        try:
            template = _ENV.get_template(f"{name}.{candidate}.j2")
            break
        except jinja2.TemplateNotFound:
            continue
    else:
        raise ValueError(
            f"prompt template not found: {name} (tried .{lang}.j2, .en.j2)"
        )
    return template.render(**_isolate_vars(name, vars))


def _isolate_vars(name: str, vars: dict[str, object]) -> dict[str, object]:
    """插值前逐参数过隔离核(白名单放行)。

    未知参数**默认扫**:新增模板变量/新数据源自动继承隔离,不需要接线
    (设计稿 §4/§5.2)。只在命中时记指标与日志,且**不记原文**。
    """
    out: dict[str, object] = {}
    for key, value in vars.items():
        if is_trusted_var(key):
            out[key] = value
            continue
        value, hits = isolate_tree(value)
        for pattern in hits:
            record_prompt_isolation("render", key, pattern)
        if hits:
            logger.warning(
                "prompt isolation: template=%s var=%s patterns=%s",
                name, key, ",".join(hits),
            )
        out[key] = value
    return out
