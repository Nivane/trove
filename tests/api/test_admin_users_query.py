"""``GET /v1/admin/users`` 参数化列表 —— 契约测试。

契约:``?q=&role=&status=&sort=&order=&limit=&offset=`` →
``{"users": [...既有全部字段..., "datasources": [...]], "total": <过滤后总数>}``
—— 授权按整页用户一次批量取回(管理台不再逐人请求,N+1 消失),
非法枚举值 400 并说明原因,无参调用保持向后兼容。
"""

from __future__ import annotations

import pytest


async def _mk_user(client, username: str, **fields) -> dict:
    """经用户 API 建号(与真实管理台同路径),返回创建后的用户体。"""
    resp = await client.post(
        "/v1/admin/users", json={"username": username, "password": "pw", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── 过滤 ─────────────────────────────────────────────────


class TestUsersQueryFilters:
    async def test_q_matches_username_and_display_name_case_insensitive(self, client):
        await _mk_user(client, "Alice")
        await _mk_user(client, "carol", display_name="Dana Wu")

        by_username = (await client.get("/v1/admin/users?q=ALI")).json()
        assert [u["username"] for u in by_username["users"]] == ["Alice"]
        assert by_username["total"] == 1

        by_display = (await client.get("/v1/admin/users?q=wu")).json()
        assert [u["username"] for u in by_display["users"]] == ["carol"]
        assert by_display["total"] == 1

        none = (await client.get("/v1/admin/users?q=zzz")).json()
        assert none["users"] == [] and none["total"] == 0

    async def test_q_matches_non_ascii_display_name(self, client):
        await _mk_user(client, "wang", display_name="王小明")
        body = (await client.get("/v1/admin/users?q=小明")).json()
        assert [u["username"] for u in body["users"]] == ["wang"]

    async def test_q_is_literal_substring_not_like_wildcard(self, client):
        # `_` 是 LIKE 通配符,但 q 语义是字面子串:搜 user_1 不该命中 userX1
        await _mk_user(client, "user_one")
        await _mk_user(client, "userXone")
        body = (await client.get("/v1/admin/users?q=user_one")).json()
        assert [u["username"] for u in body["users"]] == ["user_one"]

    async def test_role_is_exact_not_substring(self, client):
        await _mk_user(client, "carol", role="analyst")
        body = (await client.get("/v1/admin/users?role=analyst")).json()
        assert [u["username"] for u in body["users"]] == ["carol"]
        assert body["total"] == 1
        # 精确匹配:role=user 不含 analyst / admin 里的子串
        users = (await client.get("/v1/admin/users?role=user")).json()
        assert [u["username"] for u in users["users"]] == ["bob"]

    async def test_status_active_and_disabled(self, client):
        carol = await _mk_user(client, "carol")
        await client.patch(f"/v1/admin/users/{carol['id']}", json={"disabled": True})

        active = (await client.get("/v1/admin/users?status=active")).json()
        assert sorted(u["username"] for u in active["users"]) == ["admin", "bob"]
        assert active["total"] == 2

        disabled = (await client.get("/v1/admin/users?status=disabled")).json()
        assert [u["username"] for u in disabled["users"]] == ["carol"]
        assert disabled["total"] == 1

    async def test_status_nogrant_means_zero_grants(self, client, auth_service):
        bob = await auth_service.authenticate("bob", "bobpw")
        await client.put(
            f"/v1/admin/users/{bob['id']}/datasources",
            json={"datasources": ["test_db"]},
        )
        await _mk_user(client, "carol")  # 无授权
        # admin 无授权,carol 无授权,bob 有 → nogrant = {admin, carol}
        body = (await client.get("/v1/admin/users?status=nogrant")).json()
        assert sorted(u["username"] for u in body["users"]) == ["admin", "carol"]
        assert body["total"] == 2

    async def test_filters_combine_as_and(self, client):
        await _mk_user(client, "carol", role="analyst", display_name="Carol")
        await _mk_user(client, "dave", role="analyst")
        body = (await client.get("/v1/admin/users?role=analyst&q=carol")).json()
        assert [u["username"] for u in body["users"]] == ["carol"]
        assert body["total"] == 1


# ── 排序 / 分页 ──────────────────────────────────────────


class TestUsersQuerySortPaging:
    async def test_sort_username_both_orders(self, client):
        await _mk_user(client, "zed")
        asc = (await client.get(
            "/v1/admin/users?sort=username&order=asc")).json()["users"]
        assert [u["username"] for u in asc] == ["admin", "bob", "zed"]
        desc = (await client.get(
            "/v1/admin/users?sort=username&order=desc")).json()["users"]
        assert [u["username"] for u in desc] == ["zed", "bob", "admin"]

    async def test_sort_created_at_default_is_desc(self, client):
        await _mk_user(client, "zed")
        body = (await client.get("/v1/admin/users")).json()  # 默认 created_at desc
        assert body["users"][0]["username"] == "zed"
        stamps = [u["created_at"] for u in body["users"]]
        assert stamps == sorted(stamps, reverse=True)

        asc = (await client.get(
            "/v1/admin/users?sort=created_at&order=asc")).json()["users"]
        assert [u["username"] for u in asc] == ["admin", "bob", "zed"]

    async def test_sort_disabled_and_role(self, client):
        carol = await _mk_user(client, "carol", role="analyst")
        await client.patch(f"/v1/admin/users/{carol['id']}", json={"disabled": True})

        by_disabled = (await client.get(
            "/v1/admin/users?sort=disabled&order=asc")).json()["users"]
        assert [u["disabled"] for u in by_disabled] == [False, False, True]

        by_role = (await client.get(
            "/v1/admin/users?sort=role&order=asc")).json()["users"]
        assert [u["role"] for u in by_role] == ["admin", "analyst", "user"]

    async def test_limit_offset_and_total(self, client):
        for name in ("u1", "u2", "u3", "u4"):
            await _mk_user(client, name)  # admin + bob + u1..u4 = 6

        page1 = (await client.get(
            "/v1/admin/users?limit=2&offset=0&sort=username&order=asc")).json()
        page2 = (await client.get(
            "/v1/admin/users?limit=2&offset=2&sort=username&order=asc")).json()
        assert page1["total"] == page2["total"] == 6  # total 不受分页影响
        assert [u["username"] for u in page1["users"]] == ["admin", "bob"]
        assert [u["username"] for u in page2["users"]] == ["u1", "u2"]

        # total 是**过滤后**的计数,不是全库总数
        filtered = (await client.get("/v1/admin/users?role=user&limit=1")).json()
        assert filtered["total"] == 5 and len(filtered["users"]) == 1

        # offset 超出 → 空页,total 不变
        empty = (await client.get("/v1/admin/users?offset=100")).json()
        assert empty["users"] == [] and empty["total"] == 6

    async def test_default_limit_50_cap_200(self, client, auth_service):
        # 直接写 store 造量(绕开 260k 轮 PBKDF2,只为验证默认/上限)
        for i in range(55):
            await auth_service.store.create_user(f"bulk{i:02d}", "x")

        default = (await client.get("/v1/admin/users")).json()
        assert default["total"] == 57
        assert len(default["users"]) == 50  # 默认 limit=50

        capped = (await client.get("/v1/admin/users?limit=200")).json()
        assert len(capped["users"]) == 57  # 上限值本身合法


# ── 内联数据源授权 ───────────────────────────────────────


class TestUsersInlineDatasources:
    async def test_datasources_inlined_per_user_sorted(self, client, auth_service):
        await _mk_user(client, "carol")  # 无授权的对照
        bob = await auth_service.authenticate("bob", "bobpw")
        await client.put(
            f"/v1/admin/users/{bob['id']}/datasources",
            json={"datasources": ["test_db", "other"]},
        )
        body = (await client.get("/v1/admin/users")).json()
        by_name = {u["username"]: u for u in body["users"]}
        assert by_name["bob"]["datasources"] == ["other", "test_db"]
        assert by_name["carol"]["datasources"] == []
        assert by_name["admin"]["datasources"] == []
        # 既有字段一个不少(password_hash 永不出服务)
        assert {"id", "username", "role", "display_name", "disabled",
                "created_at", "updated_at"} <= set(by_name["carol"])
        assert "password_hash" not in by_name["carol"]

    async def test_grants_read_in_one_batch_not_per_user(
        self, client, auth_service, monkeypatch,
    ):
        """N+1 守卫:整页授权必须走批量查询,逐人读取的旧路径不许再被走到。"""
        bob = await auth_service.authenticate("bob", "bobpw")
        await auth_service.set_datasources(bob["id"], ["test_db"])

        async def _boom(user_id: int):  # pragma: no cover - 被调用即失败
            raise AssertionError("per-user datasource lookup used (N+1)")

        monkeypatch.setattr(auth_service.store, "get_user_datasources", _boom)
        body = (await client.get("/v1/admin/users")).json()
        assert {u["username"]: u["datasources"] for u in body["users"]} == {
            "admin": [], "bob": ["test_db"],
        }


# ── 向后兼容 / 非法参数 ──────────────────────────────────


class TestUsersQueryCompat:
    async def test_no_params_keeps_shape(self, client):
        resp = await client.get("/v1/admin/users")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"users", "total"}
        assert body["total"] == len(body["users"]) == 2  # admin + bob
        for u in body["users"]:
            assert {"id", "username", "role", "display_name", "disabled",
                    "created_at", "updated_at", "datasources"} <= set(u)

    async def test_empty_string_params_equal_absent(self, client):
        # 老前端习惯拼空串参数:与省略同义,不 400
        body = (await client.get(
            "/v1/admin/users?q=&role=&status=&sort=&order=")).json()
        assert body["total"] == 2


class TestUsersQueryValidation:
    @pytest.mark.parametrize("qs,param", [
        ("role=superuser", "role"),
        ("status=pending", "status"),
        ("sort=password_hash", "sort"),
        ("order=sideways", "order"),
        ("limit=0", "limit"),
        ("limit=201", "limit"),
        ("offset=-1", "offset"),
    ])
    async def test_invalid_params_400_with_reason(self, client, qs, param):
        resp = await client.get(f"/v1/admin/users?{qs}")
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "invalid" in detail and param in detail

    async def test_error_lists_allowed_values(self, client):
        detail = (await client.get(
            "/v1/admin/users?status=pending")).json()["detail"]
        assert "active" in detail and "disabled" in detail and "nogrant" in detail
