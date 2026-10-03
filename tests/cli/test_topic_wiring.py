"""CLI 主题域接线:--topic 走 REPL / --print 两条路径,/topic 在会话内改它。

不构造真 prompt_toolkit(测试无终端),把 PromptSession 换成桩 —— 被测的是
context 上的单点读写(/model 改 config.target 的同一模式),不是输入框。
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace


class _StubPrompt:
    def __init__(self, *a, **kw):
        pass


class _NullCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _repl_factory(tmp_path, monkeypatch):
    import trove.cli.app as app_mod

    monkeypatch.setattr(app_mod, "PromptSession", _StubPrompt)
    monkeypatch.chdir(tmp_path)

    class FakeManager:
        def __init__(self):
            self.calls: list[dict] = []

        async def ask_stream(self, **kw):
            self.calls.append(kw)
            for event in ():  # 空流:只需证明调用点对了
                yield event

    def build(topic=""):
        mgr = FakeManager()
        repl = app_mod.TroveREPL(session_manager=mgr, config=None, topic=topic)
        repl._session = object()
        return repl, mgr

    return build


async def test_topic_flag_is_the_initial_value(tmp_path, monkeypatch):
    repl, mgr = _repl_factory(tmp_path, monkeypatch)(topic="loans")
    assert repl._context["topic"] == "loans"
    await repl._consume_stream("每月贷款总额")
    assert mgr.calls[0]["topic"] == "loans"


async def test_topic_command_changes_what_the_stream_sends(tmp_path, monkeypatch):
    repl, mgr = _repl_factory(tmp_path, monkeypatch)()
    await repl._handle_slash("/topic loans")
    await repl._consume_stream("q1")
    assert mgr.calls[0]["topic"] == "loans"

    await repl._handle_slash("/topic clear")
    await repl._consume_stream("q2")
    assert mgr.calls[1]["topic"] == ""


async def test_repl_main_passes_topic_to_construction(monkeypatch):
    """async_main_repl 把 --topic 交给 TroveREPL(初始值),不做预检。"""
    import trove.cli.app as app_mod
    import trove.main as m

    seen: dict = {}

    class RecordingREPL:
        def __init__(self, **kw):
            seen.update(kw)

        async def run(self):
            pass

        async def cleanup(self):
            pass

    class FakeManager:
        async def start_session(self, **kw):
            return object()

    monkeypatch.setattr(app_mod, "TroveREPL", RecordingREPL)
    monkeypatch.setattr(m, "parse_args", lambda: SimpleNamespace(
        version=False, topic="loans"))
    monkeypatch.setattr(m, "_load_config", _fake_load_config)
    monkeypatch.setattr(m, "build_checkpointer", lambda home: _NullCM())
    monkeypatch.setattr(m, "create_app_components", _fake_components(FakeManager()))

    await m.async_main_repl()
    assert seen["topic"] == "loans"


async def test_print_mode_passes_topic(monkeypatch, capsys):
    """--print 路径把 --topic 原样透传给 ask_stream(域是否存在由管线拒绝)。"""
    import trove.main as m

    seen: dict = {}

    class FakeManager:
        async def start_session(self):
            return object()

        async def ask_stream(self, **kw):
            seen.update(kw)
            for event in ():
                yield event

    monkeypatch.setattr(sys, "argv", ["trove-cli"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("每月贷款"))
    monkeypatch.setattr(m, "parse_args", lambda: SimpleNamespace(
        print_mode=True, topic="loans", workflow="reflection", version=False))
    monkeypatch.setattr(m, "_load_config", _fake_load_config)
    monkeypatch.setattr(m, "build_checkpointer", lambda home: _NullCM())
    monkeypatch.setattr(m, "create_app_components", _fake_components(FakeManager()))

    await m.async_main_cli()
    assert seen["question"] == "每月贷款"
    assert seen["topic"] == "loans"
    capsys.readouterr()  # JSON payload 落到 stdout,不校验格式


async def _fake_load_config(args):
    return SimpleNamespace(home="/tmp/trove")


def _fake_components(manager):
    class _Registry:
        async def close_all(self):
            pass

    async def _build(args, config, checkpointer):
        return {
            "session_manager": manager,
            "config": SimpleNamespace(language="zh"),
            "catalog_service": None,
            "connector_registry": _Registry(),
            "session_store": None,
            "kb": None,
            "llm_gateway": None,
            "user_facts": None,
        }

    return _build
