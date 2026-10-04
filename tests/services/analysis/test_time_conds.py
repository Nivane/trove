"""``time_conds`` 半开区间契约:形状 + sqlite 实测(Date/Timestamp 等价)。

修的是一个实测 bug(N7):闭区间 ``<= end`` 在 **timestamp 列**上丢最后
一天 —— 当天 00:00 之后的行全部落在区间外;``date`` 列不受影响,于是
"同一问句、同一窗口,换一列类型结果就少一天数据"。半开 ``< end + 1 天``
是唯一让两种列类型一致的形式:

    sqlite 实测(2 行数据,均在 2024-01-31,窗口 2024-01-01 ~ 2024-01-31):
    闭区间   date 列命中 2 行 / timestamp 列命中 0 行  ← 反证
    半开区间 date 列命中 2 行 / timestamp 列命中 2 行

纪律:end 不可解析 → 回退闭区间并如实标注(不猜、不静默);
``dialect`` 是管道预留参数,当前不分支(四种方言统一按 ISO 字面量比较)。
"""

from __future__ import annotations

import sqlite3

from trove.services.analysis.engine import time_conds


class TestShape:
    def test_half_open_ops_and_value(self):
        conds = time_conds("sales.day", ("2024-01-01", "2024-01-31"))
        assert [(c["op"], c["value"]) for c in conds] == [
            (">=", "2024-01-01"), ("<", "2024-02-01")]

    def test_month_end_rolls_over(self):
        conds = time_conds("d", ("2024-04-01", "2024-04-30"))
        assert conds[1]["value"] == "2024-05-01"

    def test_year_end_rolls_over(self):
        conds = time_conds("d", ("2023-01-01", "2023-12-31"))
        assert conds[1]["value"] == "2024-01-01"

    def test_leap_day(self):
        conds = time_conds("d", ("2024-01-01", "2024-02-28"))
        assert conds[1]["value"] == "2024-02-29"

    def test_notes_mark_the_interval_kinds(self):
        conds = time_conds("d", ("2024-01-01", "2024-01-02"))
        assert "start" in conds[0]["note"]
        assert "half-open" in conds[1]["note"]

    def test_none_cases_are_empty(self):
        assert time_conds("", ("2024-01-01", "2024-01-02")) == []
        assert time_conds("d", None) == []

    def test_unparsable_end_falls_back_to_closed(self):
        conds = time_conds("d", ("2024-01-01", "上月"))
        assert [c["op"] for c in conds] == [">=", "<="]
        assert conds[1]["value"] == "上月"
        assert "fallback" in conds[1]["note"]

    def test_dialect_kwarg_accepted_and_branchless(self):
        shape = ("d", ("2024-01-01", "2024-01-31"))
        assert time_conds(*shape, dialect="mysql") == time_conds(*shape)
        assert time_conds(*shape, dialect="sqlite") == time_conds(*shape)


class TestSqliteSemantics:
    """实测而非推理:半开让 Date/Timestamp 两列等价;闭区间是反证。"""

    ROWS = [
        ("2024-01-31", "2024-01-31 09:30:00"),
        ("2024-01-31", "2024-01-31 18:00:00"),
    ]

    def _count(self, conds: list[dict], column: str) -> int:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (d DATE, ts TIMESTAMP)")
            conn.executemany("INSERT INTO t VALUES (?, ?)", self.ROWS)
            where = " AND ".join(
                f"{column} {c['op']} '{c['value']}'" for c in conds)
            row = conn.execute(f"SELECT COUNT(*) FROM t WHERE {where}").fetchone()
            return int(row[0])
        finally:
            conn.close()

    def test_half_open_equals_across_column_types(self):
        conds = time_conds("d", ("2024-01-01", "2024-01-31"))
        assert self._count(conds, "d") == 2
        assert self._count(conds, "ts") == 2  # 等价 —— 这才是对的形式

    def test_closed_interval_loses_the_timestamp_day(self):
        """反证:闭区间下 timestamp 列丢最后一天,date 列不丢 —— 不一致。"""
        closed = [
            {"field": "d", "op": ">=", "value": "2024-01-01"},
            {"field": "d", "op": "<=", "value": "2024-01-31"},
        ]
        assert self._count(closed, "d") == 2
        assert self._count(closed, "ts") == 0
