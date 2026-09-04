"""Terminal ASCII chart rendering (CLI/markdown).

Only single-measure bar/line shapes render as ASCII bars; multi-series and
pie fall back to the markdown table already emitted by the output node.
"""

from __future__ import annotations

from typing import Any

from trove.core.i18n import L


def _single_series(chart: dict[str, Any]) -> tuple[list[str], list[float]] | None:
    series = chart.get("series") or []
    if len(series) != 1:
        return None
    return list(chart.get("categories", [])), [float(v) for v in series[0].get("data", [])]


def render_ascii_bar(chart: dict[str, Any], lang: str = "zh", width: int = 24) -> str:
    """Markdown code block with horizontal bars (single measure)."""
    if not chart or chart.get("type") not in ("bar", "line"):
        return ""
    parsed = _single_series(chart)
    if parsed is None:
        return ""
    categories, values = parsed
    if not categories or not values:
        return ""
    vmax = max(abs(v) for v in values) or 1.0
    label_w = min(18, max(len(str(c)) for c in categories) or 1)
    lines = [L(lang, "```", "```")]
    for cat, val in zip(categories, values):
        width_n = int(abs(val) / vmax * width)
        bar = "█" * width_n + ("╵" if val != 0 and width_n == 0 else "")
        sign = "-" if val < 0 else " "
        lines.append(f"{str(cat)[:label_w]:<{label_w}} {sign}{bar} {val:g}")
    lines.append("```")
    title = chart.get("title")
    if title:
        lines.insert(0, f"**{L(lang, '图表', 'Chart')}**: {title}")
    return "\n".join(lines)


def render_waterfall_ascii(chart: dict[str, Any], lang: str = "zh", width: int = 24) -> str:
    """归因瀑布图 CLI 兜底:把 Δ 段画成带符号横向条(基期→当前不画中间值)。"""
    if not chart or chart.get("type") != "waterfall":
        return ""
    series = chart.get("series") or []
    if len(series) != 1:
        return ""
    categories = list(chart.get("categories", []))
    data = [float(v) for v in series[0].get("data", [])]
    if len(categories) != len(data) or len(data) < 2:
        return ""
    vmax = max(abs(v) for v in data[1:-1]) or 1.0
    label_w = min(18, max(len(str(c)) for c in categories) or 1)
    lines = [L(lang, "```", "```")]
    for cat, val in zip(categories, data):
        if val == data[0] or val == data[-1]:
            lines.append(f"{str(cat)[:label_w]:<{label_w}}  {val:g}")  # 基期/当前:只标值
            continue
        width_n = int(abs(val) / vmax * width)
        bar = "█" * width_n + ("╵" if val != 0 and width_n == 0 else "")
        sign = "-" if val < 0 else "+"
        lines.append(f"{str(cat)[:label_w]:<{label_w}} {sign}{bar} {val:g}")
    lines.append("```")
    title = chart.get("title")
    if title:
        lines.insert(0, f"**{L(lang, '图表', 'Chart')}**: {title}")
    return "\n".join(lines)
