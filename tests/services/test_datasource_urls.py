"""Datasource URL parsing tests (--datasource scheme:// forms)."""

import pytest

from trove.core.errors import DatasourceError
from trove.services.datasource.urls import build_url, parse_datasource_url


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

    def test_doris_full(self):
        cfg = parse_datasource_url("doris://root:root@127.0.0.1:9030/apboa")
        assert cfg.type == "doris"
        assert cfg.name == "apboa"
        assert cfg.connection_params == {
            "host": "127.0.0.1",
            "port": 9030,
            "user": "root",
            "password": "root",
            "database": "apboa",
        }
        # 非 postgres 业务库 → sqlite 本地向量
        assert cfg.vector_backend == "sqlite"

    def test_doris_default_port(self):
        cfg = parse_datasource_url("doris://user@localhost/mydb")
        assert cfg.connection_params["port"] == 9030

    def test_postgres_full(self):
        cfg = parse_datasource_url("postgres://trove:secret@pg:5432/trove")
        assert cfg.type == "postgres"
        assert cfg.connection_params == {
            "host": "pg",
            "port": 5432,
            "user": "trove",
            "password": "secret",
            "database": "trove",
        }
        # postgres 业务库默认向量后端 = pgvector(同实例)
        assert cfg.vector_backend == "pgvector"

    def test_postgres_default_port(self):
        cfg = parse_datasource_url("postgres://trove@pg/trove")
        assert cfg.connection_params["port"] == 5432

    def test_postgresql_alias_is_the_same_datasource(self):
        """``postgresql://``(libpq 标准拼法)与 ``postgres://`` 等价。

        归一发生在解析这一步、且归一成规范名 —— 下游只认 "postgres"
        一个值,不必到处判两个拼法(集成测试的 PG_TEST_URL 就是长拼法)。
        """
        cfg = parse_datasource_url("postgresql://trove:secret@pg:5432/trove")
        assert cfg.type == "postgres"
        assert cfg.connection_params == {
            "host": "pg", "port": 5432, "user": "trove",
            "password": "secret", "database": "trove",
        }
        assert cfg.vector_backend == "pgvector"
        assert cfg == parse_datasource_url("postgres://trove:secret@pg:5432/trove")

    def test_sqlite_file(self):
        cfg = parse_datasource_url("sqlite:///tmp/data.db")
        assert cfg.type == "sqlite"
        assert cfg.name == "sqlite"
        assert cfg.connection_params == {"path": "/tmp/data.db"}
        # 非 postgres 业务库 → sqlite 本地向量(覆盖全局 pgvector 默认)
        assert cfg.vector_backend == "sqlite"

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

    # ── 云仓形状(snowflake):账号 + 两段 path + query 参数 ──────

    def test_snowflake_full(self):
        cfg = parse_datasource_url(
            "snowflake://trove_user:s3cr3t@xy12345/FIN/PUBLIC"
            "?warehouse=COMPUTE_WH&role=ANALYST&private_key_file=/keys/trove.p8"
        )
        assert cfg.type == "snowflake"
        assert cfg.name == "FIN.PUBLIC"
        assert cfg.connection_params == {
            "account": "xy12345",
            "user": "trove_user",
            "password": "s3cr3t",
            "database": "FIN",
            "schema": "PUBLIC",
            "warehouse": "COMPUTE_WH",
            "role": "ANALYST",
            "private_key_file": "/keys/trove.p8",
        }
        assert cfg.vector_backend == "sqlite"   # 云仓旁挂不了 pgvector
        assert cfg.default is True

    def test_snowflake_bare_form(self):
        """最简形态:没账号密码、没 warehouse/role —— 缺省交给驱动。"""
        cfg = parse_datasource_url("snowflake://acct/DB/S")
        assert cfg.connection_params == {
            "account": "acct", "user": "", "password": "",
            "database": "DB", "schema": "S",
        }

    def test_snowflake_account_keeps_its_case(self):
        """``urlparse().hostname`` 会把 netloc **折成小写** —— 账号名会就此变脸。

        账号从**原始 netloc** 取(剥 userinfo、只做百分号解码),大小写原样保留。
        """
        cfg = parse_datasource_url("snowflake://user@MyOrg-Acct/DB/S")
        assert cfg.connection_params["account"] == "MyOrg-Acct"

    def test_snowflake_credentials_are_percent_decoded(self):
        cfg = parse_datasource_url("snowflake://u%40corp:p%3Aw%2Fd@acct/DB/S")
        assert cfg.connection_params["user"] == "u@corp"
        assert cfg.connection_params["password"] == "p:w/d"

    def test_snowflake_no_default_port_entry(self):
        """云仓不进 ``DEFAULT_PORTS``:那张表是「host 形状」的注册表,进去就会
        让 host 形状的解析路径接住一个它读不懂的连接串(netloc 不是主机名)。
        """
        from trove.services.datasource.urls import CLOUD_SCHEMES, DEFAULT_PORTS

        assert "snowflake" in CLOUD_SCHEMES
        assert "snowflake" not in DEFAULT_PORTS

    def test_snowflake_name_carries_the_schema(self):
        """同一个库里两个模式是两个可查单元 —— 只拿库名当名字,第二个注册会
        撞上管理端的「已存在」。"""
        a = parse_datasource_url("snowflake://acct/FIN/PUBLIC")
        b = parse_datasource_url("snowflake://acct/FIN/REPORTING")
        assert a.name == "FIN.PUBLIC"
        assert b.name == "FIN.REPORTING"

    @pytest.mark.parametrize("url", [
        "snowflake://acct/DB",            # 少一段(模式没给)
        "snowflake://acct/DB/S/EXTRA",    # 多一段
        "snowflake://acct//S",            # 空库名
        "snowflake://acct/DB/",           # 空模式名
        "snowflake:///DB/S",              # 没账号
    ])
    def test_snowflake_bad_shapes_are_refused(self, url):
        with pytest.raises(DatasourceError):
            parse_datasource_url(url)

    # ── 云仓形状(bigquery):项目 + 一段 path(dataset)+ query 参数 ──

    def test_bigquery_full(self):
        cfg = parse_datasource_url(
            "bigquery://my-project/analytics"
            "?location=EU&service_account_file=/keys/gcp.json"
        )
        assert cfg.type == "bigquery"
        assert cfg.name == "my-project.analytics"
        assert cfg.connection_params == {
            "project": "my-project",
            "dataset": "analytics",
            "location": "EU",
            "service_account_file": "/keys/gcp.json",
        }
        assert cfg.vector_backend == "sqlite"   # 云仓旁挂不了 pgvector
        assert cfg.default is True

    def test_bigquery_bare_form(self):
        """最简形态:没有 location / 凭据文件 —— 缺省交给客户端(ADC)。"""
        cfg = parse_datasource_url("bigquery://my-project/analytics")
        assert cfg.connection_params == {
            "project": "my-project", "dataset": "analytics",
        }

    def test_bigquery_project_keeps_its_case(self):
        """``urlparse().hostname`` 会把 netloc **折成小写** —— 与雪花取账号同一条:
        项目从**原始 netloc** 取(剥 userinfo、只做百分号解码),大小写原样保留。"""
        cfg = parse_datasource_url("bigquery://My-Project/analytics")
        assert cfg.connection_params["project"] == "My-Project"

    def test_bigquery_no_default_port_entry(self):
        """云仓不进 ``DEFAULT_PORTS``(那张表是「host 形状」的注册表)。"""
        from trove.services.datasource.urls import CLOUD_SCHEMES, DEFAULT_PORTS

        assert "bigquery" in CLOUD_SCHEMES
        assert "bigquery" not in DEFAULT_PORTS

    def test_bigquery_name_carries_the_dataset(self):
        """同一个项目里两个 dataset 是两个可查单元 —— 只拿项目名当名字,第二个
        注册会撞上管理端的「已存在」。"""
        a = parse_datasource_url("bigquery://proj/analytics")
        b = parse_datasource_url("bigquery://proj/reporting")
        assert a.name == "proj.analytics"
        assert b.name == "proj.reporting"

    def test_bigquery_userinfo_is_refused_not_dropped(self):
        """user:password **不是** BigQuery 的认证机制 —— 一组无效凭据出现在
        连接串里必须响亮地失败,不能静默丢掉装没看见(凭据只认
        service_account_file 或 ADC)。"""
        with pytest.raises(DatasourceError, match="not a BigQuery auth mechanism"):
            parse_datasource_url("bigquery://user:pass@my-project/analytics")

    @pytest.mark.parametrize("url", [
        "bigquery://my-project",          # 少 dataset
        "bigquery://my-project/a/b",      # 多一段
        "bigquery://my-project/",         # 空 dataset 名
        "bigquery:///analytics",          # 没项目
    ])
    def test_bigquery_bad_shapes_are_refused(self, url):
        with pytest.raises(DatasourceError):
            parse_datasource_url(url)

    def test_unknown_scheme_raises(self):
        with pytest.raises(DatasourceError):
            parse_datasource_url("oracle://x@host/db")

    def test_missing_database_raises(self):
        with pytest.raises(DatasourceError):
            parse_datasource_url("mysql://root@127.0.0.1")

    def test_invalid_port_raises(self):
        with pytest.raises(DatasourceError):
            parse_datasource_url("mysql://root@127.0.0.1:notaport/db")


class TestBuildUrl:
    """反向构造(管理端编辑对话框的预填 + 详情接口的 ``url`` 字段)。

    往返必须**逐字节**稳定:一次 parse→build 就换个拼法的话,对话框每次打开
    都会显示一个与上次不同的连接串(还可能是无效的那个)。
    """

    @pytest.mark.parametrize("url", [
        "mysql://root:root@127.0.0.1:3306/apboa",
        "postgres://trove:secret@pg:5432/trove",
        "clickhouse://default:pass@127.0.0.1:8123/events",
        "doris://root:root@127.0.0.1:9030/apboa",
        "sqlite:///tmp/data.db",
        "duckdb://:memory:",
    ])
    def test_the_existing_schemes_round_trip_unchanged(self, url):
        """老 scheme 的往返**逐字节不变** —— 云仓分支不许碰到它们。"""
        assert build_url(parse_datasource_url(url)) == url

    @pytest.mark.parametrize("url", [
        "snowflake://acct/DB/S",
        "snowflake://trove_user:s3cr3t@xy12345/FIN/PUBLIC",
        "snowflake://user@MyOrg-Acct/DB/S",
        "snowflake://acct/FIN/PUBLIC?warehouse=COMPUTE_WH",
        "snowflake://acct/FIN/PUBLIC"
        "?warehouse=COMPUTE_WH&role=ANALYST&private_key_file=/keys/trove.p8",
    ])
    def test_snowflake_round_trips_byte_for_byte(self, url):
        assert build_url(parse_datasource_url(url)) == url

    def test_snowflake_credentials_come_back_encoded(self):
        cfg = parse_datasource_url("snowflake://u%40corp:p%3Aw%2Fd@acct/DB/S")
        assert build_url(cfg) == "snowflake://u%40corp:p%3Aw%2Fd@acct/DB/S"

    def test_a_missing_identity_is_refused(self):
        """账号/库/模式是连接身份的一部分 —— 拼不出来就**报错**,不产出一个
        少了半截的连接串(那会让编辑对话框预填一条无效 URL)。"""
        from trove.core.types import DatasourceConfig

        cfg = DatasourceConfig(
            name="broken", type="snowflake",
            connection_params={"account": "", "database": "DB", "schema": "S"},
        )
        with pytest.raises(DatasourceError):
            build_url(cfg)

    @pytest.mark.parametrize("url", [
        "bigquery://my-project/analytics",
        "bigquery://My-Project/analytics",
        "bigquery://my-project/analytics?location=US",
        "bigquery://my-project/analytics"
        "?location=US&service_account_file=/keys/gcp.json",
    ])
    def test_bigquery_round_trips_byte_for_byte(self, url):
        assert build_url(parse_datasource_url(url)) == url

    def test_the_bigquery_identity_is_refused_when_incomplete(self):
        """项目/dataset 是连接身份的一部分 —— 拼不出来就报错,不产出一个少了
        半截的连接串。"""
        from trove.core.types import DatasourceConfig

        for params in (
            {"project": "", "dataset": "analytics"},
            {"project": "my-project", "dataset": ""},
        ):
            cfg = DatasourceConfig(
                name="broken", type="bigquery", connection_params=params,
            )
            with pytest.raises(DatasourceError):
                build_url(cfg)
