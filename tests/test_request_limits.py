import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.request_limits import RequestBodyLimitMiddleware
from tests.http import AUTH_HEADERS, TEST_ACCESS_KEY

MAX_BODY = 256 * 1024


@pytest.mark.parametrize("streaming", [False, True])
async def test_oversized_login_is_rejected_before_creating_a_session(streaming):
    body = json.dumps({"key": TEST_ACCESS_KEY, "unused": "x" * MAX_BODY}).encode()

    async def chunks():
        for start in range(0, len(body), 8192):
            yield body[start : start + 8192]

    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        response = await api.post(
            "/auth/login",
            content=chunks() if streaming else body,
            headers={"Content-Type": "application/json", "Origin": "http://localhost:3000"},
        )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "set-cookie" not in response.headers
    assert TEST_ACCESS_KEY not in response.text


async def test_an_oversized_declared_length_is_rejected_without_reading_any_body():
    messages = []
    receive = AsyncMock(side_effect=AssertionError("oversized body was read"))
    await server.app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"host", b"test"),
                (b"content-length", str(MAX_BODY + 1).encode()),
                (b"expect", b"100-continue"),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
        lambda message: append(messages, message),
    )
    assert messages[0]["status"] == 413
    receive.assert_not_awaited()


async def append(messages, message):
    messages.append(message)


async def test_a_stalled_body_is_rejected_without_starting_task_side_effects(monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(server, "_start_task_on_vm", start)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled():
        yield b'{"instruction":"'
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        request = asyncio.create_task(
            api.post(
                "/tasks",
                content=stalled(),
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (await api.get("/health")).status_code == 200
            response = await asyncio.wait_for(asyncio.shield(request), 6)
            assert response.status_code == 408
            assert response.headers["cache-control"] == "no-store"
            assert cancelled.is_set()
            start.assert_not_awaited()
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_authentication_still_rejects_a_protected_request_before_reading_its_body():
    async def unread():
        raise AssertionError("unauthenticated body was read")
        yield b""

    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        response = await api.post("/tasks", content=unread())
    assert response.status_code == 401


@pytest.mark.parametrize("character", ["あ", "😀"])
async def test_the_full_instruction_limit_still_accepts_escaped_unicode(monkeypatch, character):
    instruction = character * 20000
    start = AsyncMock(
        return_value=server.TaskStatus(
            session_id="accepted",
            state="completed",
            is_running=False,
            action_count=0,
            success_count=0,
            failure_count=0,
        )
    )
    monkeypatch.setattr(server, "_create_new_task", start)
    body = json.dumps({"instruction": instruction}, ensure_ascii=True).encode()
    assert len(body) < MAX_BODY
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as api:
        response = await api.post(
            "/tasks", content=body, headers={**AUTH_HEADERS, "Content-Type": "application/json"}
        )
    assert response.status_code == 200
    assert start.await_args.args[0].instruction == instruction


def http_scope(headers=(), version="1.1"):
    return {"type": "http", "http_version": version, "headers": list(headers)}


async def invoke(scope, parts, *, target=None, timeout=0.05, limit=8):
    target = target or AsyncMock()
    messages = []
    iterator = iter(parts)

    async def receive():
        return next(iterator)

    await RequestBodyLimitMiddleware(target, max_bytes=limit, read_timeout=timeout)(
        scope, receive, lambda message: append(messages, message)
    )
    return target, messages


@pytest.mark.parametrize("declared", [None, b"1", b"8"])
async def test_actual_size_is_checked_even_when_length_is_missing_or_understated(declared):
    headers = [] if declared is None else [(b"content-length", declared)]
    target, messages = await invoke(
        http_scope(headers),
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56789", "more_body": False},
        ],
    )
    assert messages[0]["status"] == 413
    target.assert_not_awaited()


async def test_exact_limit_is_replayed_once_and_disconnect_remains_available():
    received = []

    async def target(scope, receive, send):
        received.append(await receive())
        received.append(await receive())

    await invoke(
        http_scope([(b"content-length", b"0008")]),
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
            {"type": "http.disconnect"},
        ],
        target=target,
    )
    assert received == [
        {"type": "http.request", "body": b"12345678", "more_body": False},
        {"type": "http.disconnect"},
    ]


@pytest.mark.parametrize(
    "lengths,status",
    [
        ([b"-1"], 400),
        ([b""], 400),
        ([b"1x"], 400),
        ([b"1", b"1"], 400),
        ([b"1, 1"], 400),
        ([b"9" * 5000], 413),
    ],
)
async def test_invalid_or_huge_content_lengths_are_rejected_without_reading(lengths, status):
    target, messages = await invoke(
        http_scope([(b"content-length", value) for value in lengths]), []
    )
    assert messages[0]["status"] == status
    assert (b"connection", b"close") in messages[0]["headers"]
    target.assert_not_awaited()


@pytest.mark.parametrize("declared", [b"2", b"4"])
async def test_a_complete_body_must_match_its_declared_length(declared):
    target, messages = await invoke(
        http_scope([(b"content-length", declared)]),
        [
            {"type": "http.request", "body": b"abc", "more_body": False},
        ],
    )
    assert messages[0]["status"] == 400
    target.assert_not_awaited()


async def test_read_timeout_is_total_even_when_the_sender_keeps_dribbling():
    target, send = AsyncMock(), AsyncMock()
    stopped = asyncio.Event()

    async def receive():
        try:
            await asyncio.sleep(0.01)
            return {"type": "http.request", "body": b"x", "more_body": True}
        except asyncio.CancelledError:
            stopped.set()
            raise

    await RequestBodyLimitMiddleware(target, max_bytes=MAX_BODY, read_timeout=0.05)(
        http_scope(), receive, send
    )
    assert send.await_args_list[0].args[0]["status"] == 408
    assert stopped.is_set()
    target.assert_not_awaited()


async def test_disconnection_before_the_end_of_a_body_never_dispatches_the_endpoint():
    target, messages = await invoke(
        http_scope(),
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ],
    )
    target.assert_not_awaited()
    assert messages == []


async def test_read_timeout_does_not_cover_endpoint_execution_or_convert_its_errors():
    async def target(scope, receive, send):
        assert (await receive())["body"] == b"complete"
        await asyncio.sleep(0.03)
        raise TimeoutError("endpoint timeout")

    with pytest.raises(TimeoutError, match="endpoint timeout"):
        await invoke(
            http_scope(),
            [
                {"type": "http.request", "body": b"complete", "more_body": False},
            ],
            target=target,
            timeout=0.01,
        )


@pytest.mark.parametrize("kind", ["lifespan", "websocket"])
async def test_non_http_scopes_pass_through_without_consuming_input(kind):
    target, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": kind}
    await RequestBodyLimitMiddleware(target)(scope, receive, send)
    target.assert_awaited_once_with(scope, receive, send)
    receive.assert_not_awaited()


async def test_http2_rejections_omit_the_forbidden_connection_header():
    _, messages = await invoke(http_scope([(b"content-length", b"9")], version="2"), [])
    assert messages[0]["status"] == 413
    assert not any(name == b"connection" for name, _ in messages[0]["headers"])
