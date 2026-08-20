"""Session ownership: users can only touch their own sessions (404); admin sees all."""

from __future__ import annotations

import pytest


async def _new_session(client) -> str:
    resp = await client.post("/v1/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


class TestSessionScoping:
    async def test_create_session_binds_user(self, user_client):
        sid = await _new_session(user_client)
        resp = await user_client.get(f"/v1/sessions/{sid}")
        assert resp.status_code == 200
        # session user_id == the auth service id (verified via /me)
        me = await user_client.get("/v1/auth/me")
        assert resp.json()["user_id"] == str(me.json()["user"]["id"])

    async def test_list_sessions_filtered_per_user(self, api_app, user_client, client, auth_service):
        bob_sid = await _new_session(user_client)
        admin_sid = await _new_session(client)

        bob_list = (await user_client.get("/v1/sessions")).json()["sessions"]
        assert bob_sid in [s["session_id"] for s in bob_list]
        assert admin_sid not in [s["session_id"] for s in bob_list]

        admin_list = (await client.get("/v1/sessions")).json()["sessions"]
        all_ids = [s["session_id"] for s in admin_list]
        assert bob_sid in all_ids and admin_sid in all_ids


class TestCrossUserAccess:
    async def test_foreign_session_404_everywhere(self, api_app, user_client, client):
        """bob's session is invisible to a second user (404 on every op)."""
        bob_sid = await _new_session(user_client)

        # Second ordinary user: alice
        alice = await api_app.state.auth.create_user("alice", "alicepw")
        from httpx import ASGITransport, AsyncClient
        raw, _ = await api_app.state.auth.create_token(alice["id"], label="test-alice")
        transport = ASGITransport(app=api_app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": f"Bearer {raw}"},
        ) as alice_client:
            assert (await alice_client.get(f"/v1/sessions/{bob_sid}")).status_code == 404
            assert (await alice_client.delete(f"/v1/sessions/{bob_sid}")).status_code == 404
            assert (await alice_client.get(f"/v1/sessions/{bob_sid}/tasks")).status_code == 404
            assert (await alice_client.post(
                f"/v1/sessions/{bob_sid}/compact"
            )).status_code == 404
            assert (await alice_client.post(
                f"/v1/sessions/{bob_sid}/clear"
            )).status_code == 404
            chat = await alice_client.post(
                "/v1/chat", json={"session_id": bob_sid, "question": "hi"}
            )
            assert chat.status_code == 404

    async def test_owner_operations_work(self, user_client):
        sid = await _new_session(user_client)
        resp = await user_client.get(f"/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert (await user_client.get(f"/v1/sessions/{sid}/tasks")).status_code == 200
        assert (await user_client.post(f"/v1/sessions/{sid}/clear")).status_code == 200

    async def test_admin_can_access_foreign_session(self, user_client, client):
        sid = await _new_session(user_client)
        resp = await client.get(f"/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["user_id"] != "local"

    async def test_admin_session_list_has_all(self, client):
        assert (await client.get("/v1/sessions")).status_code == 200

    async def test_admin_sessions_endpoint(self, user_client, client):
        bob_sid = await _new_session(user_client)
        resp = await client.get("/v1/admin/sessions")
        assert resp.status_code == 200
        ids = [s["session_id"] for s in resp.json()["sessions"]]
        assert bob_sid in ids
        # filter by owner
        me = await user_client.get("/v1/auth/me")
        filtered = await client.get(f"/v1/admin/sessions?user_id={me.json()['user']['id']}")
        assert bob_sid in [s["session_id"] for s in filtered.json()["sessions"]]
