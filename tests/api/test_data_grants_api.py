"""/v1/admin/users/{id}/data-grants:行/列授权的读写 + 写前校验。

三条契约在 HTTP 面上必须成立:

* GET 是**全量现状**(按数据源分组;没有授权 → 空字典,不是 404);
* PUT 按数据源整体替换,回显**读回值**(排序/去重后的存储形态);
* 存不进去的谓词在写入前就 400 —— 一条保存成功、执行才炸的谓词会让该用户
  所有提问失败,管理端却只看到「已保存」。

校验分两层:语法(总是校验)与存在性(catalog 可达时才校验)。测试数据源
``test_db.students(id, name, grade, county)`` 由 ``sqlite_registry`` 提供。
"""
from __future__ import annotations


async def _bob_id(auth_service) -> int:
    bob = await auth_service.authenticate("bob", "bobpw")
    return bob["id"]


def _path(uid: int | str) -> str:
    return f"/v1/admin/users/{uid}/data-grants"


class TestAuthz:
    async def test_non_admin_403(self, user_client, auth_service):
        uid = await _bob_id(auth_service)
        assert (await user_client.get(_path(uid))).status_code == 403
        assert (await user_client.put(
            _path(uid),
            json={"datasource": "test_db"})).status_code == 403

    async def test_anonymous_401(self, anon_client):
        assert (await anon_client.get(_path(1))).status_code == 401
        assert (await anon_client.put(
            _path(1), json={"datasource": "test_db"})).status_code == 401


class TestRoundTrip:
    async def test_empty_state_is_empty_maps(self, client, auth_service):
        uid = await _bob_id(auth_service)
        body = (await client.get(_path(uid))).json()
        assert body == {
            "row_filters": {}, "blocked_columns": {}, "admin_exempt": False}

    async def test_put_and_get_roundtrip(self, client, auth_service):
        uid = await _bob_id(auth_service)
        resp = await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "county = 'Orange'"}],
            "blocked_columns": [
                {"table": "students", "columns": ["grade"]}],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "datasource": "test_db",
            "row_filters": {"students": "county = 'Orange'"},
            "blocked_columns": {"students": ["grade"]},
        }
        assert (await client.get(_path(uid))).json()["row_filters"] == {
            "test_db": {"students": "county = 'Orange'"}}

    async def test_echo_is_normalized_storage_value(self, client, auth_service):
        """列名去重 + 排序;谓词首尾空白裁掉。"""
        uid = await _bob_id(auth_service)
        resp = await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "  county = 'Orange'  "}],
            "blocked_columns": [
                {"table": "students", "columns": ["grade", "grade", "name"]}],
        })
        assert resp.json()["blocked_columns"] == {
            "students": ["grade", "name"]}
        assert resp.json()["row_filters"] == {"students": "county = 'Orange'"}

    async def test_put_replaces_only_named_datasource(
            self, client, auth_service, api_app):
        """按数据源整体替换:另一个数据源的授权原样保留。"""
        uid = await _bob_id(auth_service)
        await api_app.state.auth.set_data_grants(
            uid, "other_db", {"t": "x = 1"}, {"t": ["c"]})
        await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "county = 'Orange'"}],
        })
        body = (await client.get(_path(uid))).json()
        assert body["row_filters"] == {
            "test_db": {"students": "county = 'Orange'"},
            "other_db": {"t": "x = 1"},
        }
        # 该源的被禁列是本请求没声明的部分 —— 整体替换把它清空了
        assert body["blocked_columns"] == {
            "other_db": {"t": ["c"]}}
        # other_db 不在注册表里也能读回(读路径不做注册校验)
        grants = await api_app.state.auth.get_row_grants(uid)
        assert grants["other_db"] == {"t": "x = 1"}

    async def test_empty_lists_clear_that_datasource(self, client, auth_service):
        uid = await _bob_id(auth_service)
        await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "county = 'Orange'"}],
            "blocked_columns": [
                {"table": "students", "columns": ["grade"]}],
        })
        resp = await client.put(_path(uid), json={"datasource": "test_db"})
        assert resp.status_code == 200
        assert resp.json()["row_filters"] == {}
        assert resp.json()["blocked_columns"] == {}

    async def test_qualified_and_function_predicates_are_allowed(
            self, client, auth_service):
        uid = await _bob_id(auth_service)
        for predicate in (
            "students.county = 'Orange'",
            "LOWER(county) = 'orange'",
            "grade > 80 AND county IS NOT NULL",
        ):
            resp = await client.put(_path(uid), json={
                "datasource": "test_db",
                "row_filters": [{"table": "students", "predicate": predicate}],
            })
            assert resp.status_code == 200, (predicate, resp.text)


class TestValidation:
    async def _put(self, client, auth_service, **body):
        uid = await _bob_id(auth_service)
        payload = {"datasource": "test_db"}
        payload.update(body)
        return await client.put(_path(uid), json=payload)

    async def test_unparseable_predicate_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "students", "predicate": "county = "}])
        assert resp.status_code == 400
        assert "解析" in resp.json()["detail"]

    async def test_multi_statement_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "students", "predicate": "1 = 1; DROP TABLE students"}])
        assert resp.status_code == 400
        assert "单条" in resp.json()["detail"]

    async def test_subquery_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "students",
             "predicate": "id IN (SELECT id FROM students)"}])
        assert resp.status_code == 400
        assert "子查询" in resp.json()["detail"]

    async def test_other_table_qualifier_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "students", "predicate": "other.county = 'Orange'"}])
        assert resp.status_code == 400
        assert "其他表" in resp.json()["detail"]

    async def test_unknown_column_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "students", "predicate": "nope = 1"}])
        assert resp.status_code == 400
        assert "nope" in resp.json()["detail"]

    async def test_unknown_table_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "ghost", "predicate": "x = 1"}])
        assert resp.status_code == 400
        assert "ghost" in resp.json()["detail"]

    async def test_duplicate_table_400(self, client, auth_service):
        resp = await self._put(client, auth_service, row_filters=[
            {"table": "students", "predicate": "county = 'Orange'"},
            {"table": "students", "predicate": "grade > 80"}])
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    async def test_blocked_unknown_column_400(self, client, auth_service):
        resp = await self._put(client, auth_service, blocked_columns=[
            {"table": "students", "columns": ["grade", "nope"]}])
        assert resp.status_code == 400
        assert "nope" in resp.json()["detail"]

    async def test_blocked_star_rejected(self, client, auth_service):
        resp = await self._put(client, auth_service, blocked_columns=[
            {"table": "students", "columns": ["*"]}])
        assert resp.status_code == 400
        assert "逐列" in resp.json()["detail"]

    async def test_blocked_empty_columns_400(self, client, auth_service):
        resp = await self._put(client, auth_service, blocked_columns=[
            {"table": "students", "columns": []}])
        assert resp.status_code == 400

    async def test_failed_validation_writes_nothing(self, client, auth_service):
        """400 之后存储保持原状(校验在执行面之前,不是写一半再回滚)。"""
        uid = await _bob_id(auth_service)
        await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "county = 'Orange'"}],
        })
        await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [{"table": "students", "predicate": "!!"}],
        })
        assert (await client.get(_path(uid))).json()["row_filters"] == {
            "test_db": {"students": "county = 'Orange'"}}

    async def test_unknown_datasource_404(self, client, auth_service):
        uid = await _bob_id(auth_service)
        resp = await client.put(_path(uid), json={"datasource": "nope_db"})
        assert resp.status_code == 404

    async def test_unknown_user_404(self, client):
        assert (await client.get(_path(9999))).status_code == 404
        assert (await client.put(
            _path(9999), json={"datasource": "test_db"})).status_code == 404


class TestAdminExemption:
    async def test_admin_get_reports_exempt(self, client, auth_service):
        admin = await auth_service.authenticate("admin", "adminpw")
        body = (await client.get(_path(admin["id"]))).json()
        assert body["admin_exempt"] is True
        assert body["row_filters"] == {} and body["blocked_columns"] == {}

    async def test_put_on_admin_rejected(self, client, auth_service):
        """管理员豁免:存一条永远不生效的授权比报错更糟(降级后才"突然生效")。"""
        admin = await auth_service.authenticate("admin", "adminpw")
        resp = await client.put(_path(admin["id"]), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "county = 'Orange'"}],
        })
        assert resp.status_code == 400
        assert "豁免" in resp.json()["detail"]


class TestAudit:
    async def test_audit_records_saved_grants(self, client, auth_service):
        uid = await _bob_id(auth_service)
        await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [
                {"table": "students", "predicate": "county = 'Orange'"}],
            "blocked_columns": [
                {"table": "students", "columns": ["grade"]}],
        })
        entries = await auth_service.list_audit(action="admin.data_grant.set")
        assert len(entries) == 1
        assert entries[0]["username"] == "admin"
        assert entries[0]["details"] == {
            "user_id": uid,
            "datasource": "test_db",
            "row_filters": {"students": "county = 'Orange'"},
            "blocked_columns": {"students": ["grade"]},
        }

    async def test_validation_failure_is_not_audited_as_success(
            self, client, auth_service):
        uid = await _bob_id(auth_service)
        await client.put(_path(uid), json={
            "datasource": "test_db",
            "row_filters": [{"table": "students", "predicate": "!!"}],
        })
        assert await auth_service.list_audit(
            action="admin.data_grant.set") == []
