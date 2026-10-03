"""POST /v1/chat 的 ``topic`` 参数(HTTP 契约)。

会话层的收敛与缓存隔离在 ``tests/agent/test_session_topics.py`` 里守住;这里
只钉 HTTP 这一米:请求体里的 ``topic`` 真的进了管道(不再是一个被忽略的
字段),并且两个失败方向在**流里**说得出来(域不存在 / 域过期 —— 都不回落
全量,都在 done 事件的正文里点名)。

顺带钉住对外形状:``summary.topic`` 是这条答案的问数范围(空 = 未限定),
带域运行必须如实回填 —— 前端据它显示"当前主题域",审计据它记账。
"""
from __future__ import annotations

import yaml

from tests.api.test_chat import parse_sse


def _seed_topics(tmp_path, topics: list[dict]) -> None:
    """把主题域写进 api 夹具那份语义模型(conftest 的 provider 按 mtime 重读)。"""
    # KbService(project_root) 把 KB 目录算成 <root>/.trove/kb —— 夹具传的是
    # tmp_path/"kb",所以模型多套了一层;rglob 找那份唯一落盘的 semantics.yml。
    candidates = sorted(tmp_path.rglob("test_db/semantics.yml"))
    assert candidates, "测试语义模型未落盘 —— conftest 夹具形状变了?"
    path = candidates[-1]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["semantic_model"][0]["topics"] = topics
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


class TestChatTopic:
    async def test_topic_reaches_the_pipeline_and_the_summary(
        self, client, tmp_path,
    ):
        _seed_topics(tmp_path, [
            {"name": "learners", "description": "学生域", "datasets": ["students"]},
        ])
        resp = await client.post(
            "/v1/chat",
            json={
                "question": "What students are in Alameda county?",
                "topic": "learners",
            },
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1][0] == "done"
        summary = events[-1][1]["summary"]
        assert summary["topic"] == "learners"
        assert summary["matched_tables"] == ["students"]
        assert summary["sql"] == "SELECT name FROM students;"

    async def test_unknown_topic_refuses_inside_the_stream(self, client):
        """域不存在:不是 422/404 —— 流照常结束,但答案是显式拒绝。"""
        resp = await client.post(
            "/v1/chat",
            json={"question": "What students are in Alameda county?",
                  "topic": "ghost"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        types = [t for t, _ in events]
        assert types[-1] == "done"
        assert "sql" not in types  # 拒绝不携带 SQL
        assert "ghost" in events[-1][1]["content"]
        assert events[-1][1]["summary"]["topic"] == "ghost"

    async def test_stale_topic_refuses_inside_the_stream(self, client, tmp_path):
        """域在、数据集没了(域过期)→ 同样显式拒绝,不静默放开范围。"""
        _seed_topics(tmp_path, [
            {"name": "legacy", "datasets": ["dropped_table"]},
        ])
        resp = await client.post(
            "/v1/chat",
            json={"question": "What students are in Alameda county?",
                  "topic": "legacy"},
        )
        events = parse_sse(resp.text)
        assert [t for t, _ in events][-1] == "done"
        assert "legacy" in events[-1][1]["content"]

    async def test_plain_chat_is_unaffected(self, client):
        """不传 topic 的老客户端行为一字不变(summary.topic 是空串)。"""
        resp = await client.post("/v1/chat", json={"question": "hello"})
        assert parse_sse(resp.text)[-1][0] == "done"
        assert parse_sse(resp.text)[-1][1]["summary"]["topic"] == ""
