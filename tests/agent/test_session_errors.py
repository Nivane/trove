"""会话异常边界:未捕获异常必须走呈现层,原始文本只进诊断。

ERR-1 / ONB-4:``_stream_graph_run`` 的兜底 ``except`` 此前把 ``str(e)``
同时写进 content / reason / error —— 用户看到的是 provider 原文与路径。
现在边界统一 ``present_error``:用户可见四件套 + ``error_info``,原始异常
留在 ``detail.raw``。
"""

from __future__ import annotations

from trove.agent.session import SessionManager
from trove.core.config import AgentConfig
from trove.core.errors import LLMError
from trove.storage.session_store import SessionStore


class _ExplodingGraph:
    """图的第一跳就抛异常 —— 绕过 output 节点,直打会话边界。"""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def astream(self, *args, **kwargs):
        raise self._exc
        yield  # pragma: no cover — makes this an async generator


class _NoLLM:
    async def chat(self, *args, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("LLM should not be called on this path")

    async def chat_full(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("LLM should not be called on this path")


async def _error_events(tmp_home, exc: BaseException) -> list[dict]:
    config = AgentConfig(home=str(tmp_home), target="mock/model")
    store = SessionStore(home_dir=str(tmp_home))
    manager = SessionManager(
        config=config,
        session_store=store,
        graphs={"reflection": _ExplodingGraph(exc)},
        llm_gateway=_NoLLM(),
    )
    try:
        session = await manager.start_session(project_cwd="/tmp/p1")
        return [
            event
            async for event in manager.ask_stream(
                session=session,
                question="q",
                workflow_name="reflection",
            )
        ]
    finally:
        await store.dispose()


class TestExceptionBoundary:
    async def test_llm_error_becomes_structured_model_failure(self, tmp_home):
        exc = LLMError(
            message="LLM call failed after 3 attempts: invalid api key sk-secret",
        )
        events = await _error_events(tmp_home, exc)

        final = events[-1]
        assert final["type"] == "error"
        info = final["error_info"]
        assert info["kind"] == "model"
        assert info["retryable"] is False
        assert [a["href"] for a in info["actions"]] == ["/admin/model-config"]
        # summary 与事件顶层同源同形
        assert final["summary"]["error_info"] == info

        # 用户可见面不含 provider 原文;原文留在 detail.raw
        assert "api key" not in final["content"]
        assert "api key" not in final["summary"]["error"]
        assert "api key" not in final["summary"]["reason"]
        assert "api key" in info["detail"]["raw"]
        assert final["summary"]["error"] == final["summary"]["reason"] == info["title"]

    async def test_generic_exception_hides_path_from_user_fields(self, tmp_home):
        events = await _error_events(tmp_home, RuntimeError("/secret/path"))

        final = events[-1]
        info = final["error_info"]
        assert final["type"] == "error"
        assert "/secret/path" not in final["content"]
        assert "/secret/path" not in final["summary"]["error"]
        assert "/secret/path" not in final["summary"]["reason"]
        assert "/secret/path" in info["detail"]["raw"]
        assert info["detail"]["node"] == "workflow"
        # 未知异常仍给用户可行动的话,不是空白
        assert info["title"] and info["suggestion"]
