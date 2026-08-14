"""Core type definitions tests."""

import uuid

from trove.core.types import (
    Message,
    Session,
    QueryResult,
    SchemaInfo,
    TableInfo,
    ColumnInfo,
    Capabilities,
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
