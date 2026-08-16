"""lint_kb 枚举比对的空值处理:空串/空白取值是数据噪声,不报缺口。"""

from types import SimpleNamespace

from scripts.lint_kb import check_enums


class FakeAdapter:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql):
        return SimpleNamespace(rows=self._rows)


async def test_empty_and_none_values_not_flagged():
    """DB 里的 None 与空串取值不计入枚举缺口(无含义可写)。"""
    adapter = FakeAdapter(rows=[[None], [""], [" "], ["A"]])
    payloads = {"t": {"enums": {"c": "A=含义"}}}
    issues = await check_enums(adapter, payloads)
    assert issues == []


async def test_genuinely_missing_values_still_flagged():
    adapter = FakeAdapter(rows=[["A"], ["B"]])
    payloads = {"t": {"enums": {"c": "A=含义"}}}
    issues = await check_enums(adapter, payloads)
    assert len(issues) == 1
    assert "B" in issues[0]
