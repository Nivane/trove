""".env loading tests for the CLI entry points."""

import os
from types import SimpleNamespace

from trove.main import _load_config


class TestDotenvLoading:
    async def test_env_file_is_loaded(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("TROVE_TEST_KEY=hello123\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TROVE_TEST_KEY", raising=False)

        await _load_config(SimpleNamespace(config=None, model=None))

        assert os.environ["TROVE_TEST_KEY"] == "hello123"

    async def test_existing_env_not_overridden(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("TROVE_TEST_KEY=fromfile\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TROVE_TEST_KEY", "fromenv")

        await _load_config(SimpleNamespace(config=None, model=None))

        assert os.environ["TROVE_TEST_KEY"] == "fromenv"
