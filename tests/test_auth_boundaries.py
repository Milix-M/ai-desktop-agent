"""Persisted keys, session eviction and WebSocket shutdown preserve access policy."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import HTTPConnection

from ai_desktop_agent.server import auth
from ai_desktop_agent.server.auth import COOKIE_NAME, AccessControl, AuthenticationMiddleware
from ai_desktop_agent.server.browser_access import ManagementNetworkMiddleware
from ai_desktop_agent.server.persistence import StorageError
from tests.http import TEST_ACCESS_KEY


@pytest.mark.parametrize("damage", ["invalid-utf8", "directory"])
def test_unreadable_persisted_key_is_retained_until_the_operator_repairs_it(
    tmp_path, monkeypatch, damage
):
    monkeypatch.delenv("AGENT_ACCESS_TOKEN")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = tmp_path / "auth/access-key"
    path.parent.mkdir()
    if damage == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff\xfe")
    with pytest.raises(StorageError, match="接続キーを読み込めません"):
        AccessControl.from_environment()
    if damage == "directory":
        assert path.is_dir() and list(path.iterdir()) == []
        path.rmdir()
    else:
        assert path.read_bytes() == b"\xff\xfe"
    path.write_text(TEST_ACCESS_KEY + "\n")
    assert AccessControl.from_environment().valid_key(TEST_ACCESS_KEY)


def cookie_connection(token):
    return HTTPConnection(
        {"type": "websocket", "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())]}
    )


def test_session_capacity_revokes_the_oldest_cookie_without_revoking_newer_cookies():
    access = AccessControl(TEST_ACCESS_KEY)
    tokens = []
    try:
        first = access.new_session()
        tokens.append(first)
        valid, original = access.authenticate(cookie_connection(first))
        assert valid and original is not None and not original.revoked.is_set()
        for _ in range(auth.MAX_SESSIONS):
            tokens.append(access.new_session())
        assert original.revoked.is_set()
        assert access.authenticate(cookie_connection(first)) == (False, None)
        assert all(access.authenticate(cookie_connection(token))[0] for token in tokens[1:])
        assert access.valid_key(TEST_ACCESS_KEY)
    finally:
        for token in tokens:
            access.revoke(token)


@pytest.mark.parametrize("application_closes_first", [False, True])
async def test_logout_and_application_cleanup_send_only_one_terminal_websocket_message(
    application_closes_first,
):
    access = auth.get_access()
    token = access.new_session()
    messages = asyncio.Queue()
    incoming = asyncio.Queue()
    cleaned = asyncio.Event()

    async def application(scope, receive, send):
        try:
            await send({"type": "websocket.accept"})
            if application_closes_first:
                await send({"type": "websocket.close", "code": 1000})
            await asyncio.Event().wait()
        finally:
            # Applications commonly close their connection while releasing resources.
            await send({"type": "websocket.close", "code": 1000})
            cleaned.set()

    task = asyncio.create_task(
        AuthenticationMiddleware(application)(
            cookie_connection(token).scope, incoming.get, messages.put
        )
    )
    try:
        assert await asyncio.wait_for(messages.get(), 1) == {"type": "websocket.accept"}
        if application_closes_first:
            assert await asyncio.wait_for(messages.get(), 1) == {
                "type": "websocket.close",
                "code": 1000,
            }
        access.revoke(token)
        await asyncio.wait_for(task, 1)
        assert cleaned.is_set()
        if not application_closes_first:
            assert await asyncio.wait_for(messages.get(), 1) == {
                "type": "websocket.close",
                "code": 1008,
                "reason": "ログインが必要です",
            }
        assert messages.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        access.revoke(token)


@pytest.mark.parametrize("protocol", ["http", "websocket"])
async def test_missing_peer_address_cannot_bypass_management_network_policy(protocol):
    calls = []

    async def protected_application(scope, receive, send):
        calls.append(scope)
        pytest.fail("request without a peer address reached the protected application")

    middleware = ManagementNetworkMiddleware(protected_application)
    if protocol == "http":
        async with AsyncClient(
            transport=ASGITransport(app=middleware, client=None), base_url="http://test"
        ) as client:
            response = await client.get("/tasks", headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 403
        assert response.text == "Management network required"
    else:
        messages = asyncio.Queue()
        incoming = asyncio.Queue()
        await middleware(
            {"type": "websocket", "path": "/ws", "headers": [], "client": None},
            incoming.get,
            messages.put,
        )
        assert messages.get_nowait() == {"type": "websocket.close", "code": 1008}
        assert messages.empty()
    assert calls == []
