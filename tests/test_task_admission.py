"""Task bursts must not retain unbounded requests or prevent emergency controls."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import task_admission
from tests.http import AUTH_HEADERS, TEST_ACCESS_KEY, AuthenticatedClient


@pytest.fixture
def now(monkeypatch):
    value = [0.0]
    monkeypatch.setattr(
        task_admission, "_task_admission", task_admission.TaskAdmission(clock=lambda: value[0])
    )
    return value


def accepted():
    return server.TaskStatus(
        session_id="accepted",
        state="completed",
        is_running=False,
        action_count=0,
        success_count=0,
        failure_count=0,
    )


async def test_excess_tasks_are_rejected_while_existing_requests_are_waiting(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def blocked(*args):
        nonlocal count
        count += 1
        if count == 8:
            entered.set()
        await release.wait()
        return accepted()

    monkeypatch.setattr(server, "_create_new_task", blocked)
    stop = Mock()
    monkeypatch.setattr(server, "_active_session", SimpleNamespace(id="live", stop=stop))
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        requests = [
            asyncio.create_task(api.post("/tasks", json={"instruction": "report"}))
            for _ in range(8)
        ]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(
                api.post("/tasks", json={"instruction": "overflow"}), 0.2
            )
            assert response.status_code == 429
            assert int(response.headers["retry-after"]) >= 1
            assert response.headers["cache-control"] == "no-store"
            assert count == 8
            assert (await api.get("/health")).status_code == 200
            assert (await api.post("/tasks/current/stop")).status_code == 200
            stop.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
        assert (await api.post("/tasks", json={"instruction": "after drain"})).status_code == 200


async def test_task_bursts_are_limited_even_when_each_request_finishes(monkeypatch, now):
    start = AsyncMock(return_value=accepted())
    monkeypatch.setattr(server, "_create_new_task", start)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        for _ in range(60):
            assert (await api.post("/tasks", json={"instruction": "report"})).status_code == 200
        response = await api.post("/tasks", json={"instruction": "overflow"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert start.await_count == 60
        now[0] = 59.1
        response = await api.post("/tasks", json={"instruction": "still full"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"
        now[0] = 60.0
        assert (await api.post("/tasks", json={"instruction": "recovered"})).status_code == 200
    assert start.await_count == 61


async def test_new_sessions_and_forwarded_addresses_do_not_reset_the_limit(monkeypatch, now):
    monkeypatch.setattr(server, "_create_new_task", AsyncMock(return_value=accepted()))
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        for _ in range(60):
            assert (await api.post("/tasks", json={"instruction": "report"})).status_code == 200
        api.headers.pop("authorization")
        assert (await api.post("/auth/login", json={"key": TEST_ACCESS_KEY})).status_code == 200
        response = await api.post(
            "/tasks", json={"instruction": "overflow"}, headers={"X-Forwarded-For": "192.0.2.17"}
        )
        assert response.status_code == 429
        assert (await api.post("/auth/logout")).status_code == 200


async def test_unauthenticated_submissions_do_not_consume_operator_capacity(monkeypatch, now):
    monkeypatch.setattr(server, "_create_new_task", AsyncMock(return_value=accepted()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        for _ in range(61):
            assert (await api.post("/tasks", json={"instruction": "report"})).status_code == 401
        for _ in range(60):
            response = await api.post(
                "/tasks", json={"instruction": "report"}, headers=AUTH_HEADERS
            )
            assert response.status_code == 200


@pytest.mark.parametrize("version", ["1.1", "2"])
async def test_overload_rejection_does_not_read_the_body_and_preserves_cors(monkeypatch, version):
    monkeypatch.setattr(task_admission, "MAX_PENDING_TASK_SUBMISSIONS", 0)
    receive, send = AsyncMock(side_effect=AssertionError("body consumed")), AsyncMock()
    await server.app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": version,
            "method": "POST",
            "scheme": "http",
            "path": "/tasks",
            "raw_path": b"/tasks",
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"host", b"test"),
                (b"origin", b"http://localhost:3000"),
                (b"authorization", AUTH_HEADERS["Authorization"].encode()),
            ],
        },
        receive,
        send,
    )
    response = send.await_args_list[0].args[0]
    assert response["status"] == 429
    headers = dict(response["headers"])
    assert headers[b"access-control-allow-origin"] == b"http://localhost:3000"
    assert headers[b"cache-control"] == b"no-store"
    assert (b"connection" in headers) is (version == "1.1")
    receive.assert_not_awaited()


@pytest.mark.parametrize("failure", ["exception", "cancellation", "invalid_json", "disconnect"])
async def test_failed_or_abandoned_requests_release_their_pending_capacity(monkeypatch, failure):
    entered = asyncio.Event()

    async def fail(*args):
        entered.set()
        if failure == "cancellation":
            await asyncio.Event().wait()
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(server, "_create_new_task", fail)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        for _ in range(8):
            entered.clear()
            if failure == "invalid_json":
                assert (await api.post("/tasks", content=b"{")).status_code == 422
            elif failure == "disconnect":

                async def broken():
                    raise httpx.ReadError("client disconnected")
                    yield b""

                with pytest.raises(httpx.ReadError):
                    await api.post("/tasks", content=broken())
            elif failure == "exception":
                with pytest.raises(RuntimeError, match="controlled failure"):
                    await api.post("/tasks", json={"instruction": "report"})
            else:
                request = asyncio.create_task(api.post("/tasks", json={"instruction": "report"}))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                finally:
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await request
        monkeypatch.setattr(server, "_create_new_task", AsyncMock(return_value=accepted()))
        assert (await api.post("/tasks", json={"instruction": "after failure"})).status_code == 200


async def test_cancelled_storage_checks_keep_capacity_until_the_worker_finishes(monkeypatch):
    entered, release, guard = threading.Event(), threading.Event(), threading.Lock()
    count = 0
    monkeypatch.setattr(task_admission, "MAX_PENDING_TASK_SUBMISSIONS", 2)

    def check_writable():
        nonlocal count
        with guard:
            count += 1
            if count == 2:
                entered.set()
        assert release.wait(5)

    monkeypatch.setattr(server, "_store", SimpleNamespace(check_writable=check_writable))
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        requests = [
            asyncio.create_task(api.post("/tasks", json={"instruction": "report"}))
            for _ in range(2)
        ]
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(2):
                for request in requests:
                    request.cancel()
                await asyncio.sleep(0)
            response = await asyncio.wait_for(
                api.post("/tasks", json={"instruction": "overflow"}), 0.2
            )
            assert response.status_code == 429
            assert count == 2
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
        monkeypatch.setattr(server, "_create_new_task", AsyncMock(return_value=accepted()))
        assert (await api.post("/tasks", json={"instruction": "after drain"})).status_code == 200
