"""Prompt templates — bilingual Jinja2 templates under trove/prompts/.

Public API:
    render(name, lang="en", **vars) -> str
"""

from trove.prompts.loader import render

__all__ = ["render"]
