"""Jinja2 prompt template loader.

Prompts live in trove/prompts/<node>/<name>.<lang>.j2 (lang = en | zh).
`render` picks the template by language and falls back to English when a
language-specific file does not exist — single-language prompts ship only
``.en.j2``.

Templates are rendered with the default (lenient) Undefined: a missing
variable renders as an empty string instead of raising, matching the old
``dict.get(key, "")`` call sites.
"""

from __future__ import annotations

import re

import jinja2

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
    return template.render(**vars)
