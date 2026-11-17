from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import auth
from ai_desktop_agent.server.auth import COOKIE_NAME, AuthenticationMiddleware
from ai_desktop_agent.server.store import TaskStore
from tests.http import AUTH_HEADERS, TEST_ACCESS_KEY


@pytest.fixture
def login_server(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_sessions", {})
    return clock


async def fail_logins(client, count=10):
    for _ in range(count):
        assert (await client.post("/auth/login", json={"key": "incorrect"})).status_code == 401


async def test_repeated_key_failures_are_throttled_before_testing_a_correct_key(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        await fail_logins(api)
        response = await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert response.headers["cache-control"] == "no-store"
        assert "60秒" in response.json()["detail"]
        assert TEST_ACCESS_KEY not in response.text
        assert "set-cookie" not in response.headers
        assert api.cookies.get(COOKIE_NAME) is None


async def test_login_and_bearer_verification_share_the_same_failure_budget(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        for attempt in range(10):
            if attempt % 3 == 0:
                response = await api.post("/auth/login", json={"key": "incorrect"})
            else:
                path = "/auth/status" if attempt % 3 == 1 else "/tasks"
                response = await api.get(path, headers={"Authorization": "Bearer incorrect"})
            assert response.status_code in {200, 401}
        for path in ("/tasks", "/auth/status"):
            response = await api.get(path, headers=AUTH_HEADERS)
            assert response.status_code == 429
            assert response.headers["retry-after"] == "60"


async def test_changing_source_addresses_and_forwarding_headers_cannot_reset_attempts(login_server):
    for attempt in range(10):
        async with AsyncClient(
            transport=ASGITransport(app=server.app, client=(f"127.0.0.{attempt + 1}", 50000)),
            base_url="http://test",
        ) as api:
            response = await api.post(
                "/auth/login",
                json={"key": "incorrect"},
                headers={"X-Forwarded-For": f"192.0.2.{attempt + 1}"},
            )
            assert response.status_code == 401
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 429


async def test_throttled_attempts_do_not_extend_the_wait_and_recovery_is_automatic(login_server):
    clock = login_server
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        await fail_logins(api)
        clock[0] += 59.2
        response = await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        assert response.status_code == 429 and response.headers["retry-after"] == "1"
        clock[0] += 0.8
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 200
        assert (await api.get("/auth/status")).json() == {"authenticated": True}


async def test_a_successful_key_verification_resets_prior_failures(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        await fail_logins(api, 9)
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 200
        await fail_logins(api, 10)
        assert (await api.post("/auth/login", json={"key": "incorrect"})).status_code == 429


async def test_throttling_preserves_existing_cookie_access_and_logout(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 200
        token = api.cookies.get(COOKIE_NAME)
        await fail_logins(api)
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 429
        assert api.cookies.get(COOKIE_NAME) == token
        assert (await api.get("/tasks")).status_code == 200
        assert (
            await api.get("/tasks", headers={"Authorization": "Bearer incorrect"})
        ).status_code == 200
        assert (await api.get("/auth/status")).json() == {"authenticated": True}
        assert (await api.post("/auth/logout")).status_code == 200
        assert (await api.get("/auth/status")).json() == {"authenticated": False}
        assert (await api.get("/health")).status_code == 200


async def test_requests_without_key_guesses_do_not_consume_the_failure_budget(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        for _ in range(12):
            assert (await api.get("/tasks")).status_code == 401
            assert (await api.post("/auth/login", json={})).status_code == 422
            assert (await api.post("/auth/logout")).status_code == 200
            assert (
                await api.post(
                    "/auth/login",
                    json={"key": "incorrect"},
                    headers={"Origin": "https://evil.example"},
                )
            ).status_code == 403
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 200


async def test_websocket_bearer_guesses_consume_the_shared_budget(login_server):
    target = AsyncMock()
    middleware = AuthenticationMiddleware(target)
    for _ in range(10):
        send = AsyncMock()
        await middleware(
            {
                "type": "websocket",
                "path": "/ws",
                "headers": [(b"authorization", b"Bearer incorrect")],
            },
            AsyncMock(),
            send,
        )
        send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})
    target.assert_not_called()
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 429


async def test_throttled_websockets_never_reach_the_application_but_valid_cookies_do(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})
        token = api.cookies.get(COOKIE_NAME)
        await fail_logins(api)
    target = AsyncMock()
    middleware = AuthenticationMiddleware(target)
    send = AsyncMock()
    await middleware(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"authorization", f"Bearer {TEST_ACCESS_KEY}".encode())],
        },
        AsyncMock(),
        send,
    )
    target.assert_not_called()
    assert send.await_args.args[0]["code"] == 1008
    await middleware(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())],
        },
        AsyncMock(),
        AsyncMock(),
    )
    target.assert_awaited_once()


def test_parallel_key_guesses_cannot_overshoot_or_grow_the_failure_window(login_server):
    access = auth.get_access()

    def guess(_):
        try:
            assert access.verify_key("incorrect") is False
            return "failed"
        except auth.AuthenticationRateLimitError as error:
            assert error.retry_after == 60
            return "throttled"

    with ThreadPoolExecutor(max_workers=16) as workers:
        outcomes = list(workers.map(guess, range(100)))
    assert outcomes.count("failed") == 10
    assert outcomes.count("throttled") == 90
    assert len(access._key_failures) == 10


async def test_failure_window_expires_individual_attempts_instead_of_resetting_at_a_boundary(
    login_server,
):
    clock = login_server
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        for _ in range(10):
            assert (await api.post("/auth/login", json={"key": "incorrect"})).status_code == 401
            clock[0] += 6
        assert (await api.post("/auth/login", json={"key": "incorrect"})).status_code == 401
        response = await api.post("/auth/login", json={"key": "incorrect"})
        assert response.status_code == 429 and response.headers["retry-after"] == "6"


async def test_a_successful_bearer_verification_also_resets_prior_login_failures(login_server):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        await fail_logins(api, 9)
        assert (await api.get("/tasks", headers=AUTH_HEADERS)).status_code == 200
        await fail_logins(api)
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 429
