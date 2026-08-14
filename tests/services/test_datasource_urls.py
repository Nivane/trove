"""Datasource URL parsing tests (--datasource scheme:// forms)."""

import pytest

from trove.core.errors import DatasourceError
from trove.services.datasource.urls import parse_datasource_url


class TestParseDatasourceUrl:
    def test_mysql_full(self):
        cfg = parse_datasource_url("mysql://root:root@127.0.0.1:3306/apboa")
        assert cfg.type == "mysql"
        assert cfg.name == "apboa"
        assert cfg.connection_params == {
            "host": "127.0.0.1",
            "port": 3306,
            "user": "root",
            "password": "root",
            "database": "apboa",
        }
        assert cfg.default is True

    def test_mysql_default_port(self):
        cfg = parse_datasource_url("mysql://user@localhost/mydb")
        assert cfg.connection_params["port"] == 3306
        assert cfg.connection_params["password"] == ""
        assert cfg.name == "mydb"

    def test_mysql_no_credentials(self):
        cfg = parse_datasource_url("mysql://localhost/db")
        assert cfg.connection_params["user"] == ""
        assert cfg.connection_params["password"] == ""

    def test_clickhouse_full(self):
        cfg = parse_datasource_url("clickhouse://default:pass@127.0.0.1:8123/events")
        assert cfg.type == "clickhouse"
        assert cfg.name == "events"
        assert cfg.connection_params == {
            "host": "127.0.0.1",
            "port": 8123,
            "user": "default",
            "password": "pass",
            "database": "events",
        }

    def test_clickhouse_default_port(self):
        cfg = parse_datasource_url("clickhouse://default@localhost/events")
        assert cfg.connection_params["port"] == 8123

    def test_sqlite_file(self):
        cfg = parse_datasource_url("sqlite:///tmp/data.db")
        assert cfg.type == "sqlite"
        assert cfg.name == "sqlite"
        assert cfg.connection_params == {"path": "/tmp/data.db"}

    def test_sqlite_memory(self):
        cfg = parse_datasource_url("sqlite://:memory:")
        assert cfg.connection_params == {"path": ":memory:"}

    def test_duckdb_file(self):
        cfg = parse_datasource_url("duckdb:///tmp/data.duckdb")
        assert cfg.type == "duckdb"
        assert cfg.name == "duckdb"
        assert cfg.connection_params == {"path": "/tmp/data.duckdb"}

    def test_duckdb_memory(self):
        cfg = parse_datasource_url("duckdb://:memory:")
        assert cfg.connection_params == {"path": ":memory:"}

    def test_unknown_scheme_raises(self):
        with pytest.raises(DatasourceError):
            parse_datasource_url("oracle://x@host/db")

    def test_missing_database_raises(self):
        with pytest.raises(DatasourceError):
            parse_datasource_url("mysql://root@127.0.0.1")

    def test_invalid_port_raises(self):
        with pytest.raises(DatasourceError):
            parse_datasource_url("mysql://root@127.0.0.1:notaport/db")
