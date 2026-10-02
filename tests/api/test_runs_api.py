"""Run replay endpoint tests — ``GET /v1/runs/{run_id}``.

三件事各自成组:两级数据源(traces.jsonl 主用 / 会话 metadata.summary
兜底,``source`` 如实标注)、归属裁决(foreign → 404,且**在读数之前**)、
结构化字段纪律(prompt / 工具观测 / 状态快照原文一律不出口)。
"""

from __future__ import annotations

from trove.core.types import Message

RUN_ID = "11111111-1111-1111-1111-111111111111"
FOREIGN_RUN_ID = "22222222-2222-2222-2222-222222222222"


async def _seed_run(
    api_app, *, user_id: str, run_id: str, summary: dict | None = None,
) -> str:
    """落一条带 run_id 的 assistant 消息(模拟 ``_record_exchange`` 的落库形状)。"""
    manager = api_app.state.session_manager
    session = await manager.start_session(user_id=user_id)
    session.messages.append(Message(role="user", content="有多少学生?"))
    session.messages.append(Message(
        role="assistant",
        content="共 3 名学生。",
        metadata={
            "workflow": "reflection",
            "summary": {
                "run_id": run_id,
                "question": "有多少学生?",
                "sql": "SELECT COUNT(*) FROM students",
                **(summary or {}),
            },
        },
    ))
    await manager.save_session(session)
    return session.session_id


def _write_trace(
    tmp_path, *, run_id: str, session_id: str, summary: dict | None = None,
    finish: bool = True, question: str = "有多少学生?", close_span: bool = True,
) -> None:
    """把一次完整运行的 trace 事件写进临时 home(含敏感原文,用于泄漏断言)。"""
    from trove.tracing.local import add_event, configure_trace_store

    configure_trace_store(tmp_path)
    span = f"{run_id}:1"
    add_event(run_id, {
        "kind": "run", "session_id": session_id, "question": question,
        "ts": 1750000000.0, "lang": "zh",
    })
    add_event(run_id, {
        "kind": "span_start", "span_id": span, "parent_id": None,
        "name": "gen_sql", "seq": 1, "input": {"question": "SECRET-STATE"}, "ts": 1750000000.0,
    })
    add_event(run_id, {
        "kind": "llm", "node": "gen_sql", "model": "mock/gen",
        "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
        "output": "SECRET-OUTPUT", "elapsed_ms": 12, "temperature": 0.0,
        "reasoning": "SECRET-REASONING", "parent_id": span,
        "tokens": {"prompt": 10, "completion": 5, "total": 15},
    })
    add_event(run_id, {
        "kind": "tool", "name": "validate_sql",
        "arguments": {"sql": "SECRET-ARGS"}, "observation": "SECRET-OBS",
        "parent_id": span,
    })
    if close_span:
        add_event(run_id, {
            "kind": "span_end", "span_id": span, "output": {"sql": "SECRET-STATE2"},
            "elapsed_ms": 120, "tokens": {"prompt": 10, "completion": 5, "total": 15},
        })
    if finish:
        add_event(run_id, {"kind": "finish", "summary": summary or {}})


class TestRunReplay:
    async def test_session_fallback_terminal_only(self, client, api_app, auth_service):
        """trace 未配置(或已被裁剪)→ 兜底读会话摘要,source=session。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        await _seed_run(
            api_app, user_id=str(bob["id"]), run_id=RUN_ID,
            summary={"model": "mock/model", "datasource": "test_db"},
        )
        resp = await client.get(f"/v1/runs/{RUN_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "session"
        assert body["complete"] is True
        assert body["question"] == "有多少学生?"
        assert body["model"] == "mock/model"
        assert body["summary"]["sql"] == "SELECT COUNT(*) FROM students"
        # 兜底没有过程 —— 返回空轨迹,而不是编一份。
        assert body["timeline"] == []
        assert body["llm_calls"] == []
        assert body["tools"] == []
        assert body["started_at"] is None

    async def test_trace_source_structured_only(self, client, api_app, auth_service, tmp_path):
        """trace 主用:时间线/LLM/工具调用全给,但原文(prompt/观测/状态)不出口。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        session_id = await _seed_run(
            api_app, user_id=str(bob["id"]), run_id=RUN_ID,
            summary={"model": "mock/model"},
        )
        _write_trace(tmp_path, run_id=RUN_ID, session_id=session_id,
                     summary={"model": "mock/model", "verdict": "OK"})

        resp = await client.get(f"/v1/runs/{RUN_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "trace"
        assert body["complete"] is True
        assert body["session_id"] == session_id
        assert body["started_at"].startswith("2025-06-15")  # ts=1750000000 → ISO UTC
        assert body["model"] == "mock/model"
        assert body["timeline"] == [{
            "name": "gen_sql", "seq": 1, "depth": 0,
            "elapsed_ms": 120,
            "tokens": {"prompt": 10, "completion": 5, "total": 15},
            "status": "ok",
        }]
        assert body["llm_calls"] == [{
            "node": "gen_sql", "model": "mock/gen", "elapsed_ms": 12,
            "tokens": {"prompt": 10, "completion": 5, "total": 15},
        }]
        assert body["tools"] == [{"name": "validate_sql", "node": "gen_sql"}]
        # 终态以 finish 事件为准(比消息 metadata 里的更新)
        assert body["summary"]["verdict"] == "OK"
        # 原文纪律:prompt / 输出 / 推理 / 工具参数与观测 / 状态快照都不出口
        assert "SECRET" not in resp.text

    async def test_model_falls_back_to_gen_llm_call(self, client, api_app, auth_service, tmp_path):
        """旧 trace 的 summary 没有 model 键 → 退到 gen 节点的 LLM 调用记录。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        session_id = await _seed_run(api_app, user_id=str(bob["id"]), run_id=RUN_ID)
        _write_trace(tmp_path, run_id=RUN_ID, session_id=session_id, summary={"verdict": "OK"})

        body = (await client.get(f"/v1/runs/{RUN_ID}")).json()
        assert body["model"] == "mock/gen"

    async def test_incomplete_trace_marks_running_node(self, client, api_app, auth_service, tmp_path):
        """没跑到 finish(崩在中途):complete=False,未闭合的 span 如实标 running。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        session_id = await _seed_run(api_app, user_id=str(bob["id"]), run_id=RUN_ID)
        _write_trace(tmp_path, run_id=RUN_ID, session_id=session_id,
                     finish=False, close_span=False)

        body = (await client.get(f"/v1/runs/{RUN_ID}")).json()
        assert body["source"] == "trace"
        assert body["complete"] is False
        assert body["timeline"][0]["status"] == "running"
        assert body["timeline"][0]["elapsed_ms"] is None

    async def test_trace_of_another_session_is_ignored(self, client, api_app, auth_service, tmp_path):
        """trace 的会话对不上 → 不当轨迹用(宁缺毋假),退回终态摘要。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        await _seed_run(api_app, user_id=str(bob["id"]), run_id=RUN_ID)
        _write_trace(tmp_path, run_id=RUN_ID, session_id="other-session")

        body = (await client.get(f"/v1/runs/{RUN_ID}")).json()
        assert body["source"] == "session"
        assert body["timeline"] == []

    async def test_trace_configured_but_empty_falls_back(self, client, api_app, auth_service, tmp_path):
        """trace store 配置了、但这个 run 没有事件 → 同样兜底会话摘要。"""
        from trove.tracing.local import configure_trace_store

        configure_trace_store(tmp_path)
        bob = await auth_service.authenticate("bob", "bobpw")
        await _seed_run(api_app, user_id=str(bob["id"]), run_id=RUN_ID)

        body = (await client.get(f"/v1/runs/{RUN_ID}")).json()
        assert body["source"] == "session"


class TestRunReplayOwnership:
    async def test_unknown_run_404(self, client):
        resp = await client.get(f"/v1/runs/{FOREIGN_RUN_ID}")
        assert resp.status_code == 404

    async def test_foreign_run_404_even_with_trace(self, user_client, api_app, auth_service, tmp_path):
        """别人的 run:404 —— 盘上有 trace 也不给(归属裁决先于读数)。"""
        admin = await auth_service.authenticate("admin", "adminpw")
        session_id = await _seed_run(api_app, user_id=str(admin["id"]), run_id=RUN_ID)
        _write_trace(tmp_path, run_id=RUN_ID, session_id=session_id)

        resp = await user_client.get(f"/v1/runs/{RUN_ID}")
        assert resp.status_code == 404
        assert "SECRET" not in resp.text

    async def test_owner_can_read_own_run(self, user_client, api_app, auth_service):
        bob = await auth_service.authenticate("bob", "bobpw")
        await _seed_run(api_app, user_id=str(bob["id"]), run_id=RUN_ID)
        resp = await user_client.get(f"/v1/runs/{RUN_ID}")
        assert resp.status_code == 200

    async def test_admin_sees_every_run(self, client, api_app, auth_service):
        """管理员口径 = 全库(与会话端点的 admin bypass 一致)。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        await _seed_run(api_app, user_id=str(bob["id"]), run_id=RUN_ID)
        assert (await client.get(f"/v1/runs/{RUN_ID}")).status_code == 200

    async def test_requires_auth(self, anon_client):
        assert (await anon_client.get(f"/v1/runs/{RUN_ID}")).status_code == 401
