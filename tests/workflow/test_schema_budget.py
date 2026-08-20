"""Schema context budgeting tests — table-level trim with a lazy-fetch note."""

from trove.workflow.schema_budget import split_schema, trim_schema, table_signal


SCHEMA = (
    "Table: students\nColumns: id, county, grade\nApproximate rows: 100\n\n"
    "Table: loans\nColumns: id, amount, region\nApproximate rows: 5000\n\n"
    "Value hints: '北京' found in students.county"
)


class TestSplitSchema:
    def test_splits_tables_and_tail(self):
        tables, tail = split_schema(SCHEMA)
        names = [n for n, _ in tables]
        assert names == ["students", "loans"]
        assert "Value hints" in tail

    def test_empty_returns_no_tables(self):
        tables, tail = split_schema("")
        assert tables == []
        assert tail == ""


class TestTrimSchema:
    def test_small_schema_kept_verbatim(self):
        # 预算充足:原样返回(不裁剪)
        out = trim_schema(SCHEMA, budget_tokens=100000, question="贷款金额")
        assert "Table: loans" in out
        assert "Value hints" in out

    def test_tight_budget_keeps_highest_signal_table(self):
        # 预算只够一张表:问句点名的表保留,另一张列入懒加载提示
        out = trim_schema(SCHEMA, budget_tokens=20, question="北京 成绩 students")
        assert "Table: students" in out
        assert "Table: loans" not in out
        assert "lookup_schema" in out
        assert "loans" in out  # 被裁表在提示里点名

    def test_tail_always_preserved(self):
        out = trim_schema(SCHEMA, budget_tokens=20, question="北京 成绩 students")
        assert "Value hints" in out

    def test_deterministic_for_same_inputs(self):
        a = trim_schema(SCHEMA, budget_tokens=200, question="贷款 region")
        b = trim_schema(SCHEMA, budget_tokens=200, question="贷款 region")
        assert a == b

    def test_no_tables_returns_verbatim(self):
        assert trim_schema("No matching tables", 100, "q") == "No matching tables"


class TestTableSignal:
    def test_named_table_scores_highest(self):
        low = table_signal("loans", "Table: loans", "贷款")
        high = table_signal("loans", "Table: loans", "贷款 loans")
        assert high > low  # 点名(bonus 2.0)拉开差距
