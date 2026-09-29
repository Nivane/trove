"""P4 结构化迁移的**逐字节**验收:渲染文本不因迁移而改变(设计稿 §6-5/§6-6)。

迁移前,四个节点把行数据**先拼成文本**再交模板(``"\\n".join(" | ".join(...))``)。
拼完之后结构就没了——一个坏单元格命中隔离时整段预览作废(§8-2 的精度降档),
连带把 ``row_count``/``columns`` 的判断依据一起带走。迁移把值改成结构化、
模板里循环渲染,于是核能逐叶替换。

这类改动的风险不是报错,而是**悄悄变样**:没有异常、没有断言失败,只有模型
看到的提示词换了个样子。所以这里把**迁移前**的模板原文冻在测试里当参照实现,
同一份数据两条路各渲染一次,要求逐字节相同。

冻结的旧模板是"迁移前"的证据,不是可以随手同步的副本:哪天有人改了现行模板
的输出,本文件会红——那正是它存在的意义。
"""

from __future__ import annotations

import jinja2
import pytest

from trove.prompts import render

# 与 ``trove.prompts.loader._ENV`` 同参(Jinja 默认:不保留文件尾换行、
# 不 trim_blocks)—— 参照实现必须与现行渲染器同一套空白规则,否则比较的
# 是两件事。
_ENV = jinja2.Environment(autoescape=False)

# ── 迁移前的模板原文(冻结;勿随现行模板同步)────────────────────

_OLD_CONCLUSION_EN = """User question: {{ question }}
{% if time_context %}
Resolved time range: {{ time_context }}
{% endif %}
Generated SQL:
{{ sql }}
Result columns: {{ columns }}
Total rows returned: {{ total_rows }}
Rows:
{{ rows }}
{% if rows_note %}
{{ rows_note }}
{% endif %}

Answer the user's question directly in one sentence.
"""

_OLD_CONCLUSION_ZH = """用户问题: {{ question }}
{% if time_context %}
解析出的时间范围: {{ time_context }}
{% endif %}
生成的 SQL:
{{ sql }}
结果列: {{ columns }}
返回行数: {{ total_rows }}
数据行:
{{ rows }}
{% if rows_note %}
{{ rows_note }}
{% endif %}

请用一句话直接回答用户问题。
"""

_OLD_INSIGHTS_EN = """User question: {{ question }}
{% if time_context %}
Resolved time range: {{ time_context }}
{% endif %}
Generated SQL:
{{ sql }}
Result columns: {{ columns }}
Total rows returned: {{ total_rows }}
Rows:
{{ rows }}
{% if rows_note %}
{{ rows_note }}
{% endif %}

Write concise, data-grounded insights from these results.
"""

_OLD_INSIGHTS_ZH = """用户问题: {{ question }}
{% if time_context %}
解析出的时间范围: {{ time_context }}
{% endif %}
生成的 SQL:
{{ sql }}
结果列: {{ columns }}
返回行数: {{ total_rows }}
数据行:
{{ rows }}
{% if rows_note %}
{{ rows_note }}
{% endif %}

请基于这些结果写出精炼、有数据依据的洞察。"""

_OLD_CHART_EN = """User question: {{ question }}
{% if time_context %}
Resolved time range: {{ time_context }}
{% endif %}
Generated SQL:
{{ sql }}
Result columns: {{ columns }}
Total rows returned: {{ total_rows }}
Rows:
{{ rows }}

Call the plot_chart tool to decide whether a chart is warranted and its spec.
"""

_OLD_CHART_ZH = """用户问题: {{ question }}
{% if time_context %}
解析出的时间范围: {{ time_context }}
{% endif %}
生成的 SQL:
{{ sql }}
结果列: {{ columns }}
返回行数: {{ total_rows }}
数据行:
{{ rows }}

请调用 plot_chart 工具判定是否需要图表以及图表规格。
"""

_OLD_REFLECT_EN = """User question: {{ question }}
{% if evidence %}
Evidence (official hint, authoritative):
{{ evidence }}
{% endif %}{% if time_context %}
Resolved time range (authoritative):
{{ time_context }}
{% endif %}{% if schema_context %}
Schema context:
{{ schema_context }}
{% endif %}{% if sql %}
Generated SQL:
{{ sql }}
{% endif %}
Result columns: {{ columns }}
Total rows returned: {{ total_rows }}
Sample rows (first 5):
{{ sample }}

Does this result correctly answer the user's question?
Respond with OK, RETRY: <reason>, EMPTY, or NO_SQL: <reason>."""


ROWS = [["Alameda", 12], ["Orange", 7]]
COLUMNS = ["county", "count"]
Q = "How many students per county?"
SQL = "SELECT county, COUNT(*) FROM students GROUP BY county"


def _old(template_src: str, **vars) -> str:
    """迁移前的渲染方式:调用方自己拼接,模板只管插值。"""
    return _ENV.from_string(template_src).render(**vars)


def _joined(rows) -> str:
    return "\n".join(" | ".join(str(c) for c in row) for row in rows)


def _common() -> dict:
    return {
        "question": Q, "time_context": "", "sql": SQL,
        "columns": COLUMNS, "total_rows": 2,
    }


@pytest.mark.parametrize(
    ("name", "lang", "old_src"),
    [
        ("conclusion/user", "en", _OLD_CONCLUSION_EN),
        ("conclusion/user", "zh", _OLD_CONCLUSION_ZH),
        ("insights/user", "en", _OLD_INSIGHTS_EN),
        ("insights/user", "zh", _OLD_INSIGHTS_ZH),
    ],
    ids=["conclusion-en", "conclusion-zh", "insights-en", "insights-zh"],
)
class TestRowsLoopBytesIdentical:
    """conclusion / insights:``rows`` 由拼接文本改为结构化,输出逐字节不变。"""

    def test_no_rows_note(self, name, lang, old_src):
        expected = _old(old_src, rows=_joined(ROWS), rows_note="", **_common())
        assert render(name, lang=lang, rows=ROWS, rows_note="", **_common()) == expected

    def test_with_rows_note(self, name, lang, old_src):
        note = "Note: only the first 20 of 57 rows are shown."
        expected = _old(old_src, rows=_joined(ROWS), rows_note=note, **_common())
        assert render(name, lang=lang, rows=ROWS, rows_note=note, **_common()) == expected

    def test_time_context_present(self, name, lang, old_src):
        ctx = "2026-01-01 .. 2026-06-30"
        vars = {**_common(), "rows": ROWS, "rows_note": "", "time_context": ctx}
        expected = _old(old_src, **{**vars, "rows": _joined(ROWS)})
        assert render(name, lang=lang, **vars) == expected


class TestChartRowsBytesIdentical:
    def test_rows_only(self):
        expected = _old(_OLD_CHART_EN, rows=_joined(ROWS), **_common())
        assert render("chart/user", lang="en", rows=ROWS, rows_note="", **_common()) == expected

    def test_with_truncation_note(self):
        """chart 的截断警示迁移前拼在 rows 尾部,迁移后走独立变量——字节不变。"""
        note = "\nNote: only the first 20 of 57 rows shown, in query order (may be unsorted)."
        expected = _old(_OLD_CHART_EN, rows=_joined(ROWS) + note, **_common())
        assert render(
            "chart/user", lang="en", rows=ROWS, rows_note=note, **_common(),
        ) == expected


class TestReflectSampleBytesIdentical:
    """reflect 的 ``sample`` 迁移前是 ``str(row)``(Python 列表字面量)。"""

    def test_sample_rows(self):
        expected = _old(
            _OLD_REFLECT_EN, sample="\n".join(str(r) for r in ROWS[:5]),
            evidence="", schema_context="", **_common(),
        )
        assert render(
            "reflect/user", lang="en", sample=ROWS,
            evidence="", schema_context="", **_common(),
        ) == expected
