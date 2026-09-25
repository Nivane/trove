"""At-rest datasource secret encryption (services.datasource.secrets)."""

import pytest

from trove.core.errors import DatasourceError
from trove.services.datasource import secrets
from trove.services.datasource.config_store import ConfigStore
from trove.core.types import DatasourceConfig


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("TROVE_SECRET_KEY", raising=False)
    secrets.reset_cache()
    yield
    secrets.reset_cache()


def test_value_roundtrip(tmp_path):
    token = secrets.encrypt_value("hunter2", tmp_path)
    assert token.startswith("enc:v1:")
    assert "hunter2" not in token
    assert secrets.decrypt_value(token, tmp_path) == "hunter2"


def test_empty_and_non_str_pass_through(tmp_path):
    assert secrets.encrypt_value("", tmp_path) == ""
    assert secrets.encrypt_value(None, tmp_path) is None
    assert secrets.encrypt_value(3306, tmp_path) == 3306


def test_legacy_plaintext_reads_as_is(tmp_path):
    assert secrets.decrypt_value("plaintext", tmp_path) == "plaintext"


def test_key_file_created_0600(tmp_path):
    secrets.encrypt_value("x", tmp_path)
    key = tmp_path / "secret.key"
    assert key.exists()
    assert (key.stat().st_mode & 0o777) == 0o600


def test_wrong_key_fails_closed(tmp_path):
    token = secrets.encrypt_value("secret", tmp_path / "a")
    secrets.encrypt_value("other", tmp_path / "b")  # b has a different valid key
    with pytest.raises(DatasourceError, match="cannot decrypt"):
        secrets.decrypt_value(token, tmp_path / "b")


def test_missing_key_does_not_silently_regen(tmp_path):
    token = secrets.encrypt_value("secret", tmp_path)
    (tmp_path / "secret.key").unlink()
    secrets.reset_cache()  # 模拟新进程:缓存里没有旧 key
    with pytest.raises(DatasourceError, match="secret key not found"):
        secrets.decrypt_value(token, tmp_path)
    # 解密失败不得凭空生成新 key(否则旧密文永久失联)
    assert not (tmp_path / "secret.key").exists()


def test_env_key_override(tmp_path, monkeypatch):
    import base64
    import hashlib

    monkeypatch.setenv(
        "TROVE_SECRET_KEY",
        base64.urlsafe_b64encode(hashlib.sha256(b"passphrase").digest()).decode(),
    )
    secrets.reset_cache()
    token = secrets.encrypt_value("secret", tmp_path)
    secrets.reset_cache()
    assert secrets.decrypt_value(token, tmp_path) == "secret"
    # env key wins: no key file is written
    assert not (tmp_path / "secret.key").exists()


def test_config_store_encrypts_credentials_at_rest(tmp_path):
    store = ConfigStore(tmp_path / "datasources.yml")
    cfg = DatasourceConfig(
        name="a", type="mysql",
        connection_params={
            "host": "h", "port": 3306, "user": "u",
            "password": "conn-secret", "database": "d",
        },
        credentials={"password": "cred-secret"},
    )
    store.save_configs([cfg])
    raw = (tmp_path / "datasources.yml").read_text(encoding="utf-8")
    assert "conn-secret" not in raw
    assert "cred-secret" not in raw
    loaded = store.load_configs()[0]
    assert loaded.connection_params["password"] == "conn-secret"
    assert loaded.credentials["password"] == "cred-secret"
    assert loaded.connection_params["host"] == "h"  # non-secret untouched


def test_config_store_encrypts_dsn(tmp_path):
    store = ConfigStore(tmp_path / "datasources.yml")
    cfg = DatasourceConfig(
        name="a", type="postgres",
        retrieval_dsn="postgresql://u:dsn-secret@h/db",
    )
    store.save_configs([cfg])
    raw = (tmp_path / "datasources.yml").read_text(encoding="utf-8")
    assert "dsn-secret" not in raw
    assert store.load_configs()[0].retrieval_dsn == "postgresql://u:dsn-secret@h/db"


def test_config_store_legacy_plaintext_still_loads(tmp_path):
    path = tmp_path / "datasources.yml"
    path.write_text(
        "datasources:\n"
        "- id: d1\n  name: a\n  type: mysql\n"
        "  connection:\n    host: h\n    password: legacy-secret\n"
        "  credentials: {}\n  default: false\n",
        encoding="utf-8",
    )
    assert ConfigStore(path).load_configs()[0].connection_params["password"] == "legacy-secret"
