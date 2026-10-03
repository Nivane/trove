"""分析 payload → 图表/表格渲染(纯函数,零 I/O)。

瀑布图 payload 与前端 ECharts / CLI ASCII 渲染共用的形状定义在这里;
markdown 渲染仍在 ``workflow/nodes/output.py``(它持有答案排版语境)。
"""

from __future__ import annotations

from typing import Any


def waterfall_chart(
    question: str,
    baseline_label: str,
    base_total: float,
    cur_total: float,
    table: list[dict[str, Any]],
    lang: str,
) -> dict[str, Any] | None:
    """归因表 → ECharts 瀑布图 payload。

    categories: [基期, 各维度项…, 当前];series.data = [base_total, delta…,
    cur_total]。前端 ECharts 渲染;CLI 由 spark.render_waterfall_ascii 兜底。
    无维度项 → None(没有可拆的瀑布)。
    """
    zh = lang == "zh"
    dims = [str(it["dim"]) for it in table]
    if not dims:
        return None
    categories = [baseline_label] + dims + [
        ("当前" if zh else "Current"),
    ]
    data = [base_total] + [it["delta"] for it in table] + [cur_total]
    return {
        "type": "waterfall",
        "title": (question or "").strip()[:60],
        "dimension": ("维度贡献" if zh else "dimension contribution"),
        "categories": categories,
        "series": [{"name": ("Δ" if zh else "delta"), "data": data}],
        "measures": ["delta"],
    }


def ratio_waterfall_chart(
    question: str,
    lang: str,
    base_rate: float,
    effects: dict[str, float],
    cur_rate: float,
) -> dict[str, Any] | None:
    """比率分解瀑布图:基期率 → 本征 → 结构 → 交叉 → 当前率。"""
    if not effects:
        return None
    zh = lang == "zh"
    labels = (["基期", "本征效应", "结构效应", "交叉效应", "当前"]
              if zh else ["Base", "Within", "Composition", "Interaction", "Current"])
    return {
        "type": "waterfall",
        "title": (question or "").strip()[:60],
        "dimension": ("率分解" if zh else "rate decomposition"),
        "categories": labels,
        "series": [{
            "name": "Δ" if zh else "delta",
            "data": [
                base_rate,
                effects["within"],
                effects["composition"],
                effects["interaction"],
                cur_rate,
            ],
        }],
        "measures": ["delta"],
    }
