"""Skill templates — node-triggered methodology blocks.

A skill is a reusable "how-to" prompt block bound to pipeline nodes via
trigger conditions in ``manifest.yml`` (same directory). Skills are matched
deterministically — by node name plus state features — rendered with the
regular prompt loader (``trove.prompts.loader.render``, bilingual
``.en/.zh`` with fallback), and appended to the node's system prompt.

Design boundary: skills carry cross-datasource methodology (how to plan,
how to diagnose failures). Facts about a datasource belong in the KB, not
here.

Public API:
    matched_skills(node, **ctx) -> list[str]   matching skill names
    render_skills(node, lang="en", **ctx) -> str   rendered blocks, joined
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trove.prompts.loader import render

_MANIFEST_PATH = Path(__file__).parent / "manifest.yml"
_cache: list[dict] | None = None


def _load_manifest() -> list[dict]:
    """Manifest entries, cached for the process lifetime."""
    global _cache
    if _cache is None:
        data = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8")) or []
        _cache = list(data)
    return _cache


def _match_one(cond: object, value: object) -> bool:
    """One trigger field: scalar equality, or list membership (OR)."""
    if isinstance(cond, list):
        return value in cond
    return value == cond


def matched_skills(node: str, **ctx: object) -> list[str]:
    """Names of skills whose trigger conditions match node + ctx.

    A skill matches when its ``node`` trigger equals ``node`` and every
    other trigger field equals the corresponding ctx value. Skills without
    triggers never match.
    """
    out: list[str] = []
    for skill in _load_manifest():
        triggers = skill.get("triggers") or {}
        if not triggers or triggers.get("node") != node:
            continue
        if all(
            _match_one(v, ctx.get(k))
            for k, v in triggers.items()
            if k != "node"
        ):
            out.append(skill["name"])
    return out


def render_skills(node: str, lang: str = "en", **ctx: object) -> str:
    """Render all matched skill blocks for ``node``, blank-line joined.

    Returns "" when nothing matches — safe to append unconditionally.
    """
    blocks = [
        render(f"skills/{name}/system", lang=lang)
        for name in matched_skills(node, **ctx)
    ]
    return "\n\n".join(blocks)
