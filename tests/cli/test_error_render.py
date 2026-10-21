"""REPL 错误渲染:默认只给人话,``--verbose`` 才展开诊断原文(ERR-2)。"""

from __future__ import annotations

import pytest

from trove.cli.app import TroveREPL

_EVENT = {
    "type": "error",
    "content": "模型不可用。请联系管理员检查并配置模型与凭证。",
    "summary": {
        "error_info": {
            "kind": "model",
            "title": "模型不可用",
            "detail": {
                "raw": "[LLM_001] invalid api key sk-secret",
                "node": "workflow",
                "error_class": "LLM_SERVICE",
            },
        },
    },
}


class _Tui:
    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.markdown: list[str] = []

    def print_error(self, text: str) -> None:
        self.errors.append(text)

    def print_info(self, text: str) -> None:
        self.infos.append(text)

    def print_markdown(self, text: str) -> None:
        self.markdown.append(text)


@pytest.mark.asyncio
async def test_error_event_prints_user_copy_without_raw_text():
    repl = TroveREPL(verbose=False)
    tui = _Tui()
    repl._tui = tui

    await repl._render_event(_EVENT)

    assert tui.errors == [_EVENT["content"]]
    assert all("sk-secret" not in line for line in tui.infos + tui.markdown)


@pytest.mark.asyncio
async def test_verbose_shows_diagnostics_for_support():
    repl = TroveREPL(verbose=True)
    tui = _Tui()
    repl._tui = tui

    await repl._render_event(_EVENT)

    joined = "\n".join(tui.infos)
    assert "sk-secret" in joined
    assert "LLM_SERVICE" in joined
    assert "workflow" in joined


def test_verbose_flag_is_off_by_default():
    from trove.main import parse_args

    assert parse_args([]).verbose is False
    assert parse_args(["--verbose"]).verbose is True
