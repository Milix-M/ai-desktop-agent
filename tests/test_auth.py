import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import auth
from ai_desktop_agent.server.auth import COOKIE_NAME, AccessControl, AuthenticationMiddleware
from ai_desktop_agent.server.store import TaskStore
from tests.http import AUTH_HEADERS, TEST_ACCESS_KEY


@pytest.fixture
def api_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_sessions", {})


async def test_management_routes_reject_an_unauthenticated_client_before_validation(api_store):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        for method, path in [
            ("GET", "/vms"),
            ("POST", "/vms"),
            ("DELETE", "/vms/desktop"),
            ("POST", "/tasks"),
            ("GET", "/tasks"),
            ("GET", "/tasks/current"),
            ("POST", "/tasks/current/stop"),
            ("POST", "/vm/restart"),
            ("GET", "/openapi.json"),
        ]:
            response = await client.request(method, path)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/auth/status")).json() == {"authenticated": False}


async def test_login_uses_an_opaque_http_only_cookie_and_logout_revokes_it(api_store):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        assert (await client.post("/auth/login", json={"key": "wrong"})).status_code == 401
        assert (await client.post("/auth/login", json={"key": "日本語のキー"})).status_code == 401
        malformed = await client.post(
            "/auth/login",
            content=b'{"key":"\\ud800"}',
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 422
        wrong_shape = await client.post("/auth/login", json={"key": [TEST_ACCESS_KEY]})
        assert wrong_shape.status_code == 422 and TEST_ACCESS_KEY not in wrong_shape.text
        response = await client.post(
            "/auth/login",
            json={"key": TEST_ACCESS_KEY},
            headers={"Origin": "http://localhost:3000"},
        )
        assert response.status_code == 200
        assert TEST_ACCESS_KEY not in response.text + response.headers["set-cookie"]
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=43200" in cookie
        assert response.headers["access-control-allow-credentials"] == "true"
        tasks = await client.get("/tasks")
        assert tasks.status_code == 200 and tasks.headers["cache-control"] == "no-store"
        saved_cookie = client.cookies.get(COOKIE_NAME)
        assert (await client.post("/auth/logout")).status_code == 200
        assert (await client.get("/auth/status")).json() == {"authenticated": False}
        assert (
            await client.get("/tasks", headers={"Cookie": f"{COOKIE_NAME}={saved_cookie}"})
        ).status_code == 401


async def test_https_login_sets_a_secure_cookie(api_store):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="https://test"
    ) as client:
        response = await client.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        assert "Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize("previous", ["missing", "expired", "revoked"])
async def test_logout_clears_a_missing_or_already_expired_session(api_store, previous):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        session = None
        if previous != "missing":
            await client.post("/auth/login", json={"key": TEST_ACCESS_KEY})
            token = client.cookies.get(COOKIE_NAME)
            session = auth.get_access()._sessions[token]
            if previous == "expired":
                session.expires = 0
            else:
                auth.get_access().revoke(token)
        response = await client.post("/auth/logout")
        assert response.status_code == 200
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.cookies.get(COOKIE_NAME) is None
        assert (await client.get("/auth/status")).json() == {"authenticated": False}
        if session:
            assert session.revoked.is_set()


async def test_logout_still_rejects_a_foreign_origin_without_revoking_the_session(api_store):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        await client.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        token = client.cookies.get(COOKIE_NAME)
        response = await client.post("/auth/logout", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        assert client.cookies.get(COOKIE_NAME) == token
        assert (await client.get("/auth/status")).json() == {"authenticated": True}


async def test_anonymous_logout_does_not_revoke_another_clients_session(api_store):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as signed_in:
        await signed_in.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        async with AsyncClient(transport=transport, base_url="http://test") as anonymous:
            assert (await anonymous.post("/auth/logout")).status_code == 200
        assert (await signed_in.get("/auth/status")).json() == {"authenticated": True}


async def test_bearer_key_is_supported_but_query_parameters_are_not(api_store):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        assert (await client.get("/tasks", headers=AUTH_HEADERS)).status_code == 200
        assert (await client.get("/tasks", params={"token": TEST_ACCESS_KEY})).status_code == 401
        assert (
            await client.get("/tasks", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await client.post(
                "/auth/login",
                json={"key": TEST_ACCESS_KEY},
                headers={"Origin": "https://evil.example"},
            )
        ).status_code == 403


async def test_expired_and_pre_restart_sessions_are_not_accepted(api_store, monkeypatch):
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        await client.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        access = auth.get_access()
        session = access._sessions[client.cookies.get(COOKIE_NAME)]
        session.expires = 0
        assert (await client.get("/tasks")).status_code == 401
        assert session.revoked.is_set()
        await client.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        monkeypatch.setattr(auth, "_access", None)
        assert (await client.get("/tasks")).status_code == 401


@pytest.mark.parametrize("path", ["/ws", "/vnc/desktop"])
def test_unauthenticated_websockets_are_rejected_before_a_desktop_is_opened(path, monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(server, "_resolve_vm", connect)
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as error,
        TestClient(server.app, client=("127.0.0.1", 50000)).websocket_connect(path),
    ):
        pytest.fail("unauthenticated WebSocket accepted")
    assert error.value.code == 1008
    connect.assert_not_called()


@pytest.mark.parametrize("reason", ["logout", "expiry"])
async def test_session_end_closes_idle_websockets_and_cleans_up_application(reason):
    access = auth.get_access()
    token = access.new_session()
    session = access._sessions[token]
    if reason == "expiry":
        session.expires = auth.time.monotonic() + 0.05
    messages = asyncio.Queue()
    cleaned = asyncio.Event()

    async def application(scope, receive, send):
        try:
            await send({"type": "websocket.accept"})
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(
        AuthenticationMiddleware(application)(
            {
                "type": "websocket",
                "path": "/ws",
                "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())],
            },
            AsyncMock(),
            messages.put,
        )
    )
    try:
        assert (await asyncio.wait_for(messages.get(), 1))["type"] == "websocket.accept"
        if reason == "logout":
            access.revoke(token)
        message = await asyncio.wait_for(messages.get(), 1)
        assert message["type"] == "websocket.close" and message["code"] == 1008
        await asyncio.wait_for(task, 1)
        assert cleaned.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_generated_access_key_is_private_stable_and_shared_by_initializers(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_ACCESS_TOKEN")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=4) as workers:
        controls = list(workers.map(lambda _: AccessControl.from_environment(), range(8)))
    path = tmp_path / "auth/access-key"
    key = path.read_text().strip()
    assert len(key) >= 32 and path.stat().st_mode & 0o777 == 0o600
    assert all(control.valid_key(key) for control in controls)


def test_corrupt_access_key_is_not_silently_rotated(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_ACCESS_TOKEN")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = tmp_path / "auth/access-key"
    path.parent.mkdir()
    path.write_text("")
    with pytest.raises(ValueError, match="AGENT_ACCESS_TOKEN"):
        AccessControl.from_environment()
    assert path.read_text() == ""
