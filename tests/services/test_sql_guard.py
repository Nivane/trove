"""SqlFirewall (AST 级只读防火墙) 测试 — 恶意 SQL 绕过集。

防御层级说明:本模块是应用层纵深防御(可绕过),安全边界在数据库层
(只读角色/行限制)。这里的每条用例是「防 LLM 犯错 + 防脚本小子」,
不是「防国家行为体」。
"""

import pytest

from trove.services.sql.guard import check_readonly


class TestReadonlyAllowed:
    def test_plain_select(self):
        ok, reasons = check_readonly("SELECT * FROM students")
        assert ok is True
        assert reasons == []

    def test_select_with_join_group_order_limit(self):
        sql = """
        SELECT county, AVG(grade) AS avg_grade
        FROM students s
        JOIN classes c ON s.class_id = c.id
        WHERE c.name = 'A'
        GROUP BY county
        ORDER BY avg_grade DESC
        LIMIT 10
        """
        ok, _ = check_readonly(sql, dialect="sqlite")
        assert ok is True

    def test_pure_cte(self):
        ok, _ = check_readonly(
            "WITH x AS (SELECT 1 AS v) SELECT * FROM x", dialect="sqlite",
        )
        assert ok is True

    def test_union(self):
        ok, _ = check_readonly(
            "SELECT a FROM t1 UNION ALL SELECT b FROM t2", dialect="postgres",
        )
        assert ok is True

    def test_string_replace_function_is_allowed(self):
        """REPLACE() 是字符串函数,不是 REPLACE INTO 语句。"""
        ok, _ = check_readonly(
            "SELECT REPLACE(name, 'a', 'b') FROM students", dialect="mysql",
        )
        assert ok is True

    def test_mysql_into_variable_is_allowed(self):
        """SELECT ... INTO @var 是会话变量赋值(只读)。"""
        ok, _ = check_readonly("SELECT 1 INTO @var", dialect="mysql")
        assert ok is True

    def test_backtick_identifiers_via_mysql_fallback(self):
        """反引号标识符(内部 SQL 风格):默认方言解析失败后回退 mysql。"""
        ok, _ = check_readonly("SELECT `a` FROM `t`", dialect="sqlite")
        assert ok is True


class TestDmlDdlRejected:
    @pytest.mark.parametrize("sql", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (id INT)",
        "ALTER TABLE t ADD COLUMN b INT",
        "TRUNCATE TABLE t",
    ])
    def test_direct_dml_ddl(self, sql):
        ok, reasons = check_readonly(sql, dialect="sqlite")
        assert ok is False
        assert reasons

    def test_comment_split_keyword_bypass(self):
        """DEL/**/ETE 注释拆分绕过关键词正则 — AST 层必须拦截。"""
        ok, _ = check_readonly("DEL/**/ETE FROM t", dialect="mysql")
        assert ok is False

    def test_data_modifying_cte(self):
        """WITH ... (DELETE ...) SELECT — 顶层是 Select 但树内含 DML。

        关键词正则与「只查顶层」的 guard 都拦不住,必须整树扫描。
        """
        sql = "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"
        ok, reasons = check_readonly(sql, dialect="postgres")
        assert ok is False
        assert any("DELETE" in r for r in reasons)

    def test_update_inside_subquery(self):
        ok, _ = check_readonly(
            "SELECT * FROM (UPDATE t SET a=1) x", dialect="postgres",
        )
        assert ok is False

    def test_set_statement(self):
        ok, _ = check_readonly("SET session_timeout = 10", dialect="postgres")
        assert ok is False

    def test_call_stored_procedure(self):
        ok, _ = check_readonly("CALL sp_mutate()", dialect="mysql")
        assert ok is False

    def test_copy_statement(self):
        ok, _ = check_readonly("COPY t TO '/tmp/x.csv'", dialect="postgres")
        assert ok is False

    def test_merge_statement(self):
        sql = (
            "MERGE INTO t USING s ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET a = 1"
        )
        ok, _ = check_readonly(sql, dialect="postgres")
        assert ok is False

    def test_replace_into_statement(self):
        ok, _ = check_readonly("REPLACE INTO t VALUES (1)", dialect="mysql")
        assert ok is False

    def test_multi_statement_injection(self):
        ok, _ = check_readonly("SELECT 1; DELETE FROM t", dialect="sqlite")
        assert ok is False

    def test_garbage_syntax(self):
        ok, _ = check_readonly("SELEC * FORM t", dialect="sqlite")
        assert ok is False


class TestIntoOutfileRejected:
    def test_into_outfile(self):
        """MySQL SELECT ... INTO OUTFILE 写文件面。"""
        ok, _ = check_readonly(
            "SELECT * FROM t INTO OUTFILE '/tmp/x.csv'", dialect="mysql",
        )
        assert ok is False

    def test_into_dumpfile(self):
        ok, _ = check_readonly(
            "SELECT * FROM t INTO DUMPFILE '/tmp/x'", dialect="mysql",
        )
        assert ok is False


class TestDangerousFunctionsRejected:
    @pytest.mark.parametrize("sql,dialect", [
        ("SELECT SLEEP(5)", "mysql"),
        ("SELECT BENCHMARK(1000000, SHA1('x'))", "mysql"),
        ("SELECT LOAD_FILE('/etc/passwd')", "mysql"),
        ("SELECT pg_sleep(5)", "postgres"),
        ("SELECT pg_read_file('/etc/passwd')", "postgres"),
    ])
    def test_dangerous_function(self, sql, dialect):
        ok, reasons = check_readonly(sql, dialect=dialect)
        assert ok is False
        assert reasons


class TestMetaTableRejected:
    @pytest.mark.parametrize("sql,dialect", [
        ("SELECT * FROM sqlite_master", "sqlite"),
        ("SELECT * FROM sqlite_sequence", "sqlite"),
        ("SELECT * FROM information_schema.tables", "postgres"),
        ("SELECT * FROM information_schema.columns", "mysql"),
        ("SELECT * FROM pg_catalog.pg_tables", "postgres"),
        ("SELECT * FROM pg_tables", "postgres"),
    ])
    def test_metadata_recon(self, sql, dialect):
        ok, reasons = check_readonly(sql, dialect=dialect)
        assert ok is False
        assert reasons


class TestTableAllowlist:
    def test_table_in_allowlist_passes(self):
        ok, _ = check_readonly(
            "SELECT * FROM students WHERE age > 18",
            dialect="sqlite",
            allowed_tables={"students"},
        )
        assert ok is True

    def test_table_outside_allowlist_rejected(self):
        ok, reasons = check_readonly(
            "SELECT * FROM payroll WHERE salary > 100000",
            dialect="sqlite",
            allowed_tables={"students"},
        )
        assert ok is False
        assert any("payroll" in r for r in reasons)

    def test_allowlist_covers_cte_tables(self):
        ok, _ = check_readonly(
            "WITH x AS (SELECT id FROM students) SELECT * FROM x JOIN classes c ON x.id = c.id",
            dialect="sqlite",
            allowed_tables={"students", "classes"},
        )
        assert ok is True

    def test_allowlist_rejects_cte_table(self):
        ok, _ = check_readonly(
            "WITH x AS (SELECT id FROM payroll) SELECT * FROM x",
            dialect="sqlite",
            allowed_tables={"students"},
        )
        assert ok is False

    def test_metadata_rejected_even_in_allowlist(self):
        ok, _ = check_readonly(
            "SELECT * FROM sqlite_master",
            dialect="sqlite",
            allowed_tables={"sqlite_master"},
        )
        assert ok is False
