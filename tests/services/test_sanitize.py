"""错误信息脱敏测试 — 防止 DSN/密码经 SQL 报错路径泄露给 LLM 或日志。"""

from trove.services.sql.sanitize import sanitize_error_text


class TestSanitizeErrorText:
    def test_strips_url_password(self):
        out = sanitize_error_text("mysql+pymysql://user:secret123@host:3306/db")
        assert "secret123" not in out
        assert "user:***@host" in out

    def test_strips_url_password_with_special_chars(self):
        out = sanitize_error_text("postgresql://admin:p@ss:w0rd@db.internal/prod")
        assert "p@ss:w0rd" not in out

    def test_strips_password_kwarg(self):
        out = sanitize_error_text("connect failed: password=hunter2 host=localhost")
        assert "hunter2" not in out
        assert "password=***" in out

    def test_strips_pwd_and_passwd_variants(self):
        out = sanitize_error_text("err pwd=abc123; passwd=def456")
        assert "abc123" not in out
        assert "def456" not in out

    def test_plain_error_untouched(self):
        msg = "syntax error near 'x'"
        assert sanitize_error_text(msg) == msg

    def test_empty_text(self):
        assert sanitize_error_text("") == ""
        assert sanitize_error_text(None) == ""
