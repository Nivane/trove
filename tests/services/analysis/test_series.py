"""块序列纯函数单测:粒度推导 / 块窗口 / 相位块 / 行→序列 / 编译 hop。

窗口生成与 ``derive_grain`` 的判定**互逆**是核心不变量:生成的每个
块都能被认回同一粒度(否则旧块会被当噪声混进分布)。
"""

from __future__ import annotations

import pytest

from trove.services.analysis.series import (
    SeriesSpec,
    block_windows,
    compile_series_hop,
    derive_grain,
    same_phase_blocks,
    series_from_rows,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="sales", source="sales",
            fields=[
                SemanticField(name="amount", expression="sales.amount", semantic_role="measure"),
                SemanticField(name="region", expression="sales.region", semantic_role="dimension"),
                SemanticField(name="day", expression="sales.day", datatype="Date", is_time=True),
            ],
        )],
        metrics=[SemanticMetric(name="revenue", expression="SUM(sales.amount)", datasets=["sales"])],
    )


class _SL:
    def __init__(self, model: SemanticModel | None = None) -> None:
        self._model = model if model is not None else _model()

    def model(self) -> SemanticModel:
        return self._model


class TestDeriveGrain:
    def test_month_window(self):
        assert derive_grain(("2024-02-01", "2024-02-29")) == "month"   # 闰年
        assert derive_grain(("2024-01-01", "2024-01-31")) == "month"
        assert derive_grain(("2024-01-01", "2024-01-30")) is None      # 不满月

    def test_week_window(self):
        assert derive_grain(("2024-01-15", "2024-01-21")) == "week"    # 周一 + 7 天
        assert derive_grain(("2024-01-16", "2024-01-22")) is None      # 非周一起

    def test_day_window(self):
        assert derive_grain(("2024-02-01", "2024-02-01")) == "day"

    def test_unaligned_and_invalid(self):
        assert derive_grain(("2024-01-01", "2024-03-31")) is None      # 整季
        assert derive_grain(("2024-01-15", "2024-01-31")) is None      # 半月
        assert derive_grain(("2024-02-10", "2024-02-01")) is None      # 逆序
        assert derive_grain(None) is None
        assert derive_grain(("bad", "worse")) is None


class TestBlockWindows:
    def test_month_blocks_normalized_and_strictly_historical(self):
        blocks = block_windows(("2024-02-01", "2024-02-29"), "month", 3)
        assert blocks == [
            ("2023-11-01", "2023-11-30"),
            ("2023-12-01", "2023-12-31"),
            ("2024-01-01", "2024-01-31"),
        ]
        # 不含被测窗口(噪声带混入被测点会自我稀释)
        assert all(e < "2024-02-01" for _, e in blocks)

    def test_week_and_day_blocks(self):
        blocks = block_windows(("2024-01-15", "2024-01-21"), "week", 2)
        assert blocks == [("2024-01-01", "2024-01-07"), ("2024-01-08", "2024-01-14")]
        days = block_windows(("2024-02-01", "2024-02-01"), "day", 2)
        assert days == [("2024-01-30", "2024-01-30"), ("2024-01-31", "2024-01-31")]

    def test_generated_blocks_roundtrip_through_derive_grain(self):
        for window, grain in ((("2024-02-01", "2024-02-29"), "month"),
                              (("2024-01-15", "2024-01-21"), "week"),
                              (("2024-02-01", "2024-02-01"), "day")):
            for b in block_windows(window, grain, 5):
                assert derive_grain(b) == grain

    def test_invalid_inputs_empty(self):
        assert block_windows(None, "month", 3) == []
        assert block_windows(("2024-02-01", "2024-02-29"), "quarter", 3) == []
        assert block_windows(("2024-02-01", "2024-02-29"), "month", 0) == []


class TestSamePhaseBlocks:
    def test_year_shift_and_leap_clamp(self):
        blocks = same_phase_blocks(("2024-02-01", "2024-02-29"), 2)
        # shift_months 月长钳制:2024-02-29 → 2023-02-28 / 2022-02-28
        assert blocks == [("2022-02-01", "2022-02-28"), ("2023-02-01", "2023-02-28")]

    def test_invalid(self):
        assert same_phase_blocks(None, 2) == []
        assert same_phase_blocks(("2024-02-01", "2024-02-29"), 0) == []


class TestSeriesFromRows:
    def test_sorts_by_bucket_label_not_insertion_order(self):
        rows = [["2024-01", 44.0], ["2023-11", 40.0], ["2023-12", 42.0]]
        assert series_from_rows(["bucket", "net"], rows) == [
            ("2023-11", 40.0), ("2023-12", 42.0), ("2024-01", 44.0)]

    def test_timestamp_label_truncated_to_date(self):
        rows = [["2024-01-05 00:00:00", 1.0], ["2024-01-03 00:00:00", 2.0]]
        assert series_from_rows(["b", "v"], rows) == [
            ("2024-01-03", 2.0), ("2024-01-05", 1.0)]

    def test_short_rows_skipped_and_dupes_keep_last(self):
        rows = [["2024-01", 1.0], ["x"], ["2024-01", 9.0]]
        assert series_from_rows(["b", "v"], rows) == [("2024-01", 9.0)]

    def test_empty(self):
        assert series_from_rows([], []) == []


class TestCompileSeriesHop:
    def test_sqlite_sql_shape(self):
        sql = compile_series_hop(
            _SL(), ["sales"], "sqlite", "revenue",
            [{"field": "sales.day", "op": ">=", "value": "2023-11-01"}],
            time_grain="month", time_field="sales.day",
        )
        assert sql is not None
        # 桶列在 SELECT 首位、度量在末位(series_from_rows 的列契约)
        assert sql.index("strftime('%Y-%m', sales.day)") < sql.index("SUM(sales.amount)")
        assert "GROUP BY strftime('%Y-%m', sales.day)" in sql

    def test_mysql_sql_shape(self):
        sql = compile_series_hop(
            _SL(), ["sales"], "mysql", "revenue", [],
            time_grain="day", time_field="sales.day",
        )
        assert sql is not None and "DATE_FORMAT" in sql

    def test_dimensions_insert_the_bucket_after_the_dim_columns(self):
        """判定侧按维取带时仍是**一条** SQL:维度列 + bucket + 度量 ——
        ``series_source`` 按这个列契约解析(维度标签在前、桶在中、值在末)。"""
        sql = compile_series_hop(
            _SL(), ["sales"], "sqlite", "revenue", [],
            time_grain="month", time_field="sales.day", dimensions=["region"],
        )
        assert sql is not None
        select = sql.split("FROM")[0]
        assert select.index("sales.region") < select.index("strftime('%Y-%m', sales.day)")
        assert select.index("strftime('%Y-%m', sales.day)") < select.index("SUM(sales.amount)")
        assert "GROUP BY" in sql and "sales.region" in sql.split("GROUP BY")[-1]

    def test_miss_returns_none(self):
        # 未知粒度 / 未知度量 / 软 MISS 条件(未声明字段)一律 None:
        # 序列是统计取数面,骨架(缺组件)不算
        assert compile_series_hop(_SL(), ["sales"], "sqlite", "revenue", [],
                                  time_grain="quarter", time_field="sales.day") is None
        assert compile_series_hop(_SL(), ["sales"], "sqlite", "nope", [],
                                  time_grain="month", time_field="sales.day") is None
        assert compile_series_hop(_SL(), ["sales"], "sqlite", "revenue",
                                  [{"field": "sales.nope", "op": "=", "value": 1}],
                                  time_grain="month", time_field="sales.day") is None
        assert compile_series_hop(_SL(), ["sales"], "sqlite", "revenue", [],
                                  time_grain="month", time_field="") is None

    def test_no_model_returns_none(self):
        class _Empty:
            def model(self):
                return None
        assert compile_series_hop(_Empty(), ["sales"], "sqlite", "revenue", [],
                                  time_grain="month", time_field="sales.day") is None


class TestSeriesSpec:
    def test_defaults_and_frozen(self):
        spec = SeriesSpec()
        assert (spec.grain, spec.lookback, spec.mode, spec.k) == ("", 12, "trailing", 3.5)
        assert hash(spec) == hash(SeriesSpec())   # frozen → 可哈希、值相等
        with pytest.raises(Exception):
            spec.grain = "month"
