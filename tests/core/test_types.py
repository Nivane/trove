"""Core type definitions tests."""


from trove.core.types import (
    Message,
    Session,
    QueryResult,
    SchemaInfo,
    TableInfo,
    ColumnInfo,
    DatasourceConfig,
)


class TestMessage:
    def test_create_message(self):
        msg = Message(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.metadata == {}

    def test_message_with_metadata(self):
        msg = Message(
            role="assistant",
            content="result",
            metadata={"sql_generated": "SELECT 1"},
        )
        assert msg.metadata["sql_generated"] == "SELECT 1"

    def test_message_timestamp_auto(self):
        msg = Message(role="user", content="hi")
        assert msg.timestamp is not None


class TestSession:
    def test_session_defaults(self):
        s = Session()
        assert isinstance(s.session_id, str)
        assert len(s.session_id) > 0
        assert s.project_name == "default"
        assert s.messages == []
        assert s.summary is None

    def test_session_with_messages(self):
        s = Session()
        s.messages.append(Message(role="user", content="q1"))
        s.messages.append(Message(role="assistant", content="a1"))
        assert len(s.messages) == 2

    def test_session_ids_are_unique(self):
        s1 = Session()
        s2 = Session()
        assert s1.session_id != s2.session_id


class TestQueryResult:
    def test_query_result(self):
        result = QueryResult(
            columns=["name", "grade"],
            rows=[["Alice", 95], ["Bob", 88]],
            row_count=2,
            execution_time_ms=10.5,
            sql="SELECT name, grade FROM students",
            datasource="test",
        )
        assert result.columns == ["name", "grade"]
        assert result.row_count == 2
        assert result.execution_time_ms == 10.5


class TestSchemaTypes:
    def test_column_info(self):
        col = ColumnInfo(name="id", type="INTEGER", primary_key=True)
        assert col.name == "id"
        assert col.primary_key is True
        assert col.nullable is True

    def test_table_info(self):
        table = TableInfo(
            name="students",
            columns=[ColumnInfo(name="id", type="INTEGER")],
            row_count_estimate=100,
        )
        assert table.name == "students"
        assert table.row_count_estimate == 100
        assert len(table.columns) == 1

    def test_schema_info(self):
        schema = SchemaInfo(tables=[
            TableInfo(name="t1"),
            TableInfo(name="t2"),
        ])
        assert len(schema.tables) == 2


class TestDatasourceConfig:
    def test_config(self):
        cfg = DatasourceConfig(
            name="pg",
            type="postgres",
            connection_params={"host": "localhost"},
            default=True,
        )
        assert cfg.name == "pg"
        assert cfg.type == "postgres"
        assert cfg.default is True


class TestTimestampStr:
    """时间戳归一(执行画像 §6.1 的 ``last_modified`` / ``last_analyzed``)。

    与 ``positive_int`` 是同一条原则的**时间形式**:「不可得」必须说不可得,
    不能用某个看起来像值的字面量冒充。这里有两个具体的坑:

    * **MySQL 的零日期** ``0000-00-00 00:00:00`` —— 它读起来像个值,实际是
      MySQL 表示「没设置」的写法。放过去会流进 ``freshness`` 的 ``min()``,
      把整片数据的截止时间拉到公元 0 年,而且是**安静**的:用户看到的仍然是一个
      格式正确的时间戳,只是荒谬。
    * **驱动返回的是对象不是字符串** —— aiomysql / psycopg 对 DATETIME 列返回
      ``datetime``,直接塞进 ``as_of: str`` 会得到一个 object 的 repr,比 None
      更糟(它长得像有值)。
    """

    def test_datetime_becomes_iso(self):
        from datetime import datetime

        from trove.core.types import timestamp_str

        assert timestamp_str(datetime(2026, 9, 28, 12, 30, 5)) == "2026-09-28T12:30:05"

    def test_date_alone_is_kept(self):
        from datetime import date

        from trove.core.types import timestamp_str

        assert timestamp_str(date(2026, 9, 28)) == "2026-09-28"

    def test_mysql_zero_date_is_not_a_value(self):
        """MySQL 的 ``0000-00-00`` 是「没设置」,不是「公元 0 年」。"""
        from trove.core.types import timestamp_str

        assert timestamp_str("0000-00-00 00:00:00") is None
        assert timestamp_str("0000-00-00") is None

    def test_nothing_available_is_none_not_empty(self):
        from trove.core.types import timestamp_str

        assert timestamp_str(None) is None
        assert timestamp_str("") is None
        assert timestamp_str("   ") is None
        assert timestamp_str(0) is None

    def test_engine_string_passes_through_unparsed(self):
        """认不出来的字符串原样带出 —— 我们**不解析**引擎给的格式。

        解析器要覆盖各家方言的写成,而猜错的代价是谎报一个更精确的时间;
        原样带出至少是诚实的,比较时也不会比真实值更新。
        """
        from trove.core.types import timestamp_str

        assert timestamp_str("2026-09-28") == "2026-09-28"
        assert timestamp_str("202609") == "202609"
