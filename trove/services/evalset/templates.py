"""验收集生成用的确定性模板:候选骨架、自然名解析、中英文问句措辞。

语义模型的冷启动生成**不依赖 LLM** 也能产出可编译的问题:五类候选
(``metric_dim`` / ``metric_grain`` / ``metric_dim_grain`` / ``metric_filter``
/ ``metric_top_n``)的问句文本完全由已声明的 label / description /
synonyms 拼出。LLM 改写是可选装饰,失败时回落到这里的模板
(见 :mod:`trove.services.evalset.generate`)。

``GRAINS`` 与 ``services.evalset.models.GRAIN_ORDER`` 同序同值;枚举只取
前三档(``CANDIDATE_GRAINS``)——月/周/日覆盖了绝大多数问题形态,季度/年
与它们组合收益低而候选池膨胀快。聚合口径遵循语义模型自身声明的粒度边界。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 语义层支持的时间粒度(与 ``models.GRAIN_ORDER`` 同序)。
GRAINS = ("month", "week", "day", "quarter", "year")

#: 冷启动实际枚举的粒度(顺序即优先级;只取前三档)。
CANDIDATE_GRAINS = GRAINS[:3]

#: description 作为自然名的最大字符数 —— 再长就是文档而不是名字。
MAX_DESCRIPTION_CHARS = 20

_GRAIN_ZH = {
    "month": "月",
    "week": "周",
    "day": "日",
    "quarter": "季度",
    "year": "年",
}


@dataclass
class Candidate:
    """一个可编译的候选问题骨架(kind + 组件引用)。

    ``metric`` 是度量名;``dimension`` / ``field`` 是 **qualified**
    ``dataset.field`` 引用(编译器的声明式 API 要求),展示时再解析成自然名。
    ``value`` 是枚举值(原样存储写法),``top_n`` 仅 top_n 候选使用。
    """

    kind: str
    metric: str
    dimension: str = ""
    grain: str = ""
    field: str = ""
    value: str = ""
    top_n: int = 0

    def key(self) -> str:
        """稳定身份:同一组件的候选永远得到同一个 key(去重/排序用)。"""
        return "|".join(str(part) for part in (
            self.kind, self.metric, self.dimension, self.grain,
            self.field, self.value, self.top_n,
        ))


def natural_name(
    label: str, description: str, synonyms: list[str] | tuple[str, ...], name: str,
) -> str:
    """组件的自然名:``label`` → 短 ``description`` → 首个别名 → 名字美化。

    description 只在足够短(``<= 20`` 字)时使用:一整段文档放进问句会让
    模板问句读起来像拼接出来的说明,而不是用户会问的话。
    """
    label = (label or "").strip()
    if label:
        return label
    desc = (description or "").strip()
    if desc and len(desc) <= MAX_DESCRIPTION_CHARS:
        return desc
    for synonym in synonyms or ():
        text = str(synonym).strip()
        if text:
            return text
    return (name or "").replace("_", " ")


def grain_label(grain: str, lang: str) -> str:
    """粒度显示名:zh 用「月/周/日/季度/年」,其余语言用原始值。"""
    if _is_zh(lang):
        return _GRAIN_ZH.get(grain, grain)
    return grain


def format_question(cand: Candidate, labels: dict[str, str], lang: str) -> str:
    """按候选类型套中/英文模板;``labels`` 是 ref → 自然名的映射。

    五类模板**必须**带 metric/dimension/filter 的自然名 —— 评测题是给人
    确认的,``loan_amount by loan.district`` 这种技术引用不算问题文本。
    """
    metric = labels.get(cand.metric, cand.metric)
    dim = labels.get(cand.dimension, cand.dimension)
    field = labels.get(cand.field, cand.field)
    grain = grain_label(cand.grain, lang)
    zh = _is_zh(lang)

    if cand.kind == "metric_dim":
        return (f"按{dim}统计，{metric}是多少？" if zh
                else f"What is {metric} by {dim}?")
    if cand.kind == "metric_grain":
        return (f"{metric}按{grain}看是多少？" if zh
                else f"What is {metric} by {grain}?")
    if cand.kind == "metric_dim_grain":
        return (f"按{dim}和{grain}统计{metric}。" if zh
                else f"What is {metric} by {dim} and {grain}?")
    if cand.kind == "metric_filter":
        return (f"{field}为{cand.value}时，{metric}是多少？" if zh
                else f"What is {metric} when {field} is {cand.value}?")
    if cand.kind == "metric_top_n":
        return (f"{dim}按{metric}排名前 {cand.top_n} 是哪些？" if zh
                else f"Which {dim} have the highest {metric}? Top {cand.top_n}.")
    raise ValueError(f"unknown candidate kind: {cand.kind!r}")


def _is_zh(lang: str) -> bool:
    return str(lang or "").lower().startswith("zh")


__all__ = [
    "CANDIDATE_GRAINS",
    "Candidate",
    "GRAINS",
    "MAX_DESCRIPTION_CHARS",
    "format_question",
    "grain_label",
    "natural_name",
]
