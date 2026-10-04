"""/v1/admin/users/{id}/topic-grants:域级授权的读写契约(admin-only)。

三态语义在 HTTP 面上必须原样成立:

* ``null``(省略字段同义)= **取消收窄** → 可见数据源的全部域;
* ``{}`` = **收窄到零** → 一个域都不见(不是"未配置");
* ``{ds: [...]}`` = 该源的严格清单,字典里没有的源 = 该源无域。

PUT 是整表写入;回显读回值(归一化后的存储形态),不是请求体。
"""
from __future__ import annotations


async def _bob_id(auth_service) -> int:
    bob = await auth_service.authenticate("bob", "bobpw")
    return bob["id"]


class TestAuthz:
    async def test_non_admin_403(self, user_client, auth_service):
        uid = await _bob_id(auth_service)
        path = f"/v1/admin/users/{uid}/topic-grants"
        assert (await user_client.get(path)).status_code == 403
        assert (await user_client.put(
            path, json={"topic_grants": {}})).status_code == 403

    async def test_anonymous_401(self, anon_client):
        assert (await anon_client.get(
            "/v1/admin/users/1/topic-grants")).status_code == 401
        assert (await anon_client.put(
            "/v1/admin/users/1/topic-grants", json={})).status_code == 401


class TestRoundTrip:
    async def test_get_set_roundtrip(self, client, auth_service):
        uid = await _bob_id(auth_service)
        path = f"/v1/admin/users/{uid}/topic-grants"

        # 新用户 = 未配置(None,不是 {})
        assert (await client.get(path)).json()["topic_grants"] is None

        resp = await client.put(path, json={
            "topic_grants": {"financial": ["loans"], "sales": ["orders"]}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["topic_grants"] == {
            "financial": ["loans"], "sales": ["orders"]}
        assert (await client.get(path)).json()["topic_grants"] == {
            "financial": ["loans"], "sales": ["orders"]}

    async def test_null_clears_and_empty_map_stays_empty(self, client, auth_service):
        """``null`` 与 ``{}`` 是两种语义,往返后必须还是两种。"""
        uid = await _bob_id(auth_service)
        path = f"/v1/admin/users/{uid}/topic-grants"

        await client.put(path, json={"topic_grants": {"financial": ["loans"]}})
        assert (await client.put(
            path, json={"topic_grants": {}})).json()["topic_grants"] == {}
        assert (await client.get(path)).json()["topic_grants"] == {}

        assert (await client.put(
            path, json={"topic_grants": None})).json()["topic_grants"] is None
        assert (await client.get(path)).json()["topic_grants"] is None

    async def test_omitted_field_means_null(self, client, auth_service):
        """省略字段 = null(取消收窄)—— 整表写入的"缺省即清空"。

        注意与 ``{"topic_grants": {}}`` 的区别:那个是**显式**收窄到零。
        """
        uid = await _bob_id(auth_service)
        path = f"/v1/admin/users/{uid}/topic-grants"
        await client.put(path, json={"topic_grants": {"financial": ["loans"]}})
        assert (await client.put(path, json={})).json()["topic_grants"] is None
        assert (await client.get(path)).json()["topic_grants"] is None

    async def test_echo_is_normalized_storage_value(self, client, auth_service):
        """回显读回值:裁剪 / 去重 / 排序后的形态(不是请求体的原样)。"""
        uid = await _bob_id(auth_service)
        resp = await client.put(f"/v1/admin/users/{uid}/topic-grants", json={
            "topic_grants": {" financial ": ["loans", "loans", " cards "]}})
        assert resp.json()["topic_grants"] == {"financial": ["cards", "loans"]}

    async def test_per_datasource_empty_list_survives(self, client, auth_service):
        uid = await _bob_id(auth_service)
        path = f"/v1/admin/users/{uid}/topic-grants"
        await client.put(path, json={"topic_grants": {"financial": []}})
        assert (await client.get(path)).json()["topic_grants"] == {"financial": []}


class TestErrorsAndAudit:
    async def test_unknown_user_404(self, client):
        assert (await client.get(
            "/v1/admin/users/9999/topic-grants")).status_code == 404
        assert (await client.put(
            "/v1/admin/users/9999/topic-grants",
            json={"topic_grants": {}})).status_code == 404

    async def test_audit_records_saved_map(self, client, auth_service):
        uid = await _bob_id(auth_service)
        await client.put(f"/v1/admin/users/{uid}/topic-grants", json={
            "topic_grants": {"financial": ["loans"]}})
        entries = await auth_service.list_audit(action="admin.topic_grant.set")
        assert len(entries) == 1
        assert entries[0]["username"] == "admin"
        assert entries[0]["details"] == {
            "user_id": uid, "topic_grants": {"financial": ["loans"]}}

    async def test_audit_records_clearing(self, client, auth_service):
        """清空同样留痕(null 出现在审计里,而不是一条"看起来没发生"的记录)。"""
        uid = await _bob_id(auth_service)
        path = f"/v1/admin/users/{uid}/topic-grants"
        await client.put(path, json={"topic_grants": {"financial": []}})
        await client.put(path, json={"topic_grants": None})
        entries = await auth_service.list_audit(action="admin.topic_grant.set")
        assert entries[0]["details"]["topic_grants"] is None
