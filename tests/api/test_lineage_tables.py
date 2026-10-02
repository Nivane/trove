"""GET /v1/lineage/tables/{name} —— 表血缘只读包装(设计稿 P5 §4.1③)。

薄包装 ``LineageService`` 的既有读方法,零新逻辑。覆盖:

- 冷启动(表从未被查过)是真实空态:``query_log.count: 0`` / ``definitions: []``
  —— 没记录 ≠ 没依赖;
- 定义(视图/CTAS)给出 upstream/downstream/definitions(``view`` / ``ctas``)
  与逐列血缘(生产者 + 消费者);
- ``column`` 参数把 columns 收窄到一列;未知名如实回显(空血缘,不 404);
- 权限:用户面(``get_current_user`` + ``require_datasource``),非授权源 403。
"""

from __future__ import annotations

from trove.services.lineage.service import LineageService

CREATE_STUDENTS = (
    "CREATE VIEW students AS SELECT id, name, grade FROM raw_students"
)
CREATE_COUNTY = (
    "CREATE VIEW county_avg AS "
    "SELECT county, AVG(grade) AS avg_grade FROM students GROUP BY county"
)
QUERY = "SELECT county, AVG(grade) FROM students GROUP BY county"

TOP_KEYS = {
    "table", "datasource", "upstream", "downstream",
    "columns", "definitions", "query_log",
}


def _root(api_app):
    return api_app.state.kb.kb_dir.parent.parent


async def _seed(api_app) -> None:
    svc = LineageService(_root(api_app))
    await svc.ingest_definition(CREATE_STUDENTS, "test_db")
    await svc.ingest_definition(CREATE_COUNTY, "test_db")
    await svc.record_query(QUERY, "test_db")


class TestLineageColdStart:
    async def test_cold_start_empty_but_typed(self, client, api_kb):
        r = await client.get("/v1/lineage/tables/students")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == TOP_KEYS
        assert body["table"] == "students"
        assert body["datasource"] == "test_db"
        assert body["upstream"] == []
        assert body["downstream"] == []
        assert body["columns"] == []
        assert body["definitions"] == []
        assert body["query_log"] == {"count": 0, "last_at": None}

    async def test_unknown_table_is_empty_not_404(self, client, api_kb):
        r = await client.get("/v1/lineage/tables/ghost_table")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["table"] == "ghost_table"     # 未知名原样回显
        assert body["definitions"] == []
        assert body["query_log"]["count"] == 0


class TestLineageSeeded:
    async def test_upstream_downstream_and_definitions(self, client, api_app, api_kb):
        await _seed(api_app)
        r = await client.get("/v1/lineage/tables/students")
        assert r.status_code == 200, r.text
        body = r.json()

        # upstream:产出 students 的定义
        assert [d["name"] for d in body["upstream"]] == ["students"]
        assert body["upstream"][0]["kind"] == "create_view"
        assert body["upstream"][0]["sql"] == CREATE_STUDENTS

        # downstream:读 students 的定义 + 真实查询历史
        kinds = {d["kind"] for d in body["downstream"]}
        assert kinds == {"create_view", "query"}
        by_kind = {d["kind"]: d for d in body["downstream"]}
        assert by_kind["create_view"]["name"] == "county_avg"
        assert by_kind["query"]["sql"] == QUERY
        assert by_kind["query"]["runs"] == 1

        # definitions:upstream + downstream 的 DDL,kind 归一到 view/ctas
        assert body["definitions"] == [
            {"kind": "view", "sql": CREATE_STUDENTS},
            {"kind": "view", "sql": CREATE_COUNTY},
        ]

        # query_log 只统计执行过的查询(def: 分片不算)
        assert body["query_log"]["count"] == 1
        assert body["query_log"]["last_at"]

    async def test_columns_and_column_lineage(self, client, api_app, api_kb):
        await _seed(api_app)
        body = (await client.get("/v1/lineage/tables/students")).json()

        cols = {c["column"]: c for c in body["columns"]}
        assert {"id", "name", "grade"} <= set(cols)     # 定义投影的输出列
        assert "county" in cols                         # 查询历史触到的列
        # grade:生产者是 students 视图的投影,消费者是那条查询
        assert cols["grade"]["upstream"]
        assert cols["grade"]["upstream"][0]["name"] == "students"
        assert any(
            s["table"].lower() == "raw_students"
            for s in cols["grade"]["upstream"][0]["sources"]
        )
        assert [q["sql"] for q in cols["grade"]["downstream"]] == [QUERY]

    async def test_column_param_narrows(self, client, api_app, api_kb):
        await _seed(api_app)
        body = (await client.get(
            "/v1/lineage/tables/students", params={"column": "grade"},
        )).json()
        assert [c["column"] for c in body["columns"]] == ["grade"]
        assert body["columns"][0]["downstream"]

        # 未知名如实回显(空血缘),不是 404 —— 面板按「没有血缘记录」渲染
        body = (await client.get(
            "/v1/lineage/tables/students", params={"column": "missing"},
        )).json()
        assert body["columns"] == [
            {"column": "missing", "upstream": [], "downstream": []},
        ]

    async def test_table_name_case_insensitive(self, client, api_app, api_kb):
        await _seed(api_app)
        body = (await client.get("/v1/lineage/tables/STUDENTS")).json()
        assert body["table"] == "students"          # 归一为血缘库里的拼写
        assert [d["name"] for d in body["upstream"]] == ["students"]
        assert body["query_log"]["count"] == 1


class TestLineageAuth:
    async def test_anon_401(self, anon_client):
        r = await anon_client.get("/v1/lineage/tables/students")
        assert r.status_code in (401, 403)

    async def test_user_default_source_allowed_then_403_for_other(
        self, client, user_client, api_app, api_kb,
    ):
        """空 grants 只放行默认源;显式指向别的源 → 403(判定在策略层)。"""
        from trove.core.types import DatasourceConfig

        r = await user_client.get("/v1/lineage/tables/students")
        assert r.status_code == 200, r.text
        assert r.json()["datasource"] == "test_db"

        await api_app.state.connector_registry.register(DatasourceConfig(
            name="extra", type="sqlite",
            connection_params={"path": ":memory:"}, credentials={}, default=False,
        ))
        r = await user_client.get(
            "/v1/lineage/tables/students", params={"datasource": "extra"},
        )
        assert r.status_code == 403
        # admin 对任意已注册源都放行(extra 没有血缘记录 → 空态 200)
        r = await client.get(
            "/v1/lineage/tables/students", params={"datasource": "extra"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["datasource"] == "extra"
        assert r.json()["query_log"] == {"count": 0, "last_at": None}
