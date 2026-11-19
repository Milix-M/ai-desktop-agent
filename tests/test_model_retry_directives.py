"""Upstream retry directives must not cause early sends or unbounded waits."""

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm import openai_compat_provider as module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_task_budget import completion, llm_with_transport

SCHEMA = {"name": "report", "schema": {"type": "object"}}
NOW = 1_800_000_000


@pytest.mark.parametrize(
    "headers,waits",
    [
        ({"Retry-After": "5"}, [5, 5]),
        ({"Retry-After": "0"}, [0, 0]),
        ({"Retry-After": "60"}, [60, 60]),
        ({"retry-after-ms": "2500"}, [2.5, 2.5]),
        ({"retry-after-ms": "750", "Retry-After": "5"}, [0.75, 0.75]),
        ({"retry-after-ms": "invalid", "Retry-After": "4"}, [4, 4]),
        ({"retry-after-ms": "-1", "Retry-After": "4"}, [4, 4]),
        ({"Retry-After": "NaN"}, [1.5, 3]),
        ({"Retry-After": "Infinity"}, [1.5, 3]),
        ({"Retry-After": "-1"}, [1.5, 3]),
        ({"Retry-After": "not a date"}, [1.5, 3]),
        ({"Retry-After": "Fri, 01 Jan 10000 00:00:00 GMT"}, [1.5, 3]),
        ({}, [1.5, 3]),
    ],
)
async def test_retry_waits_follow_valid_headers_and_fall_back_for_invalid_values(
    headers, waits, provider_retry_sleep
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers=headers, json={"error": {"message": "busy"}})

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError):
            await provider._call("報告を確認する", SCHEMA)
        assert provider.call_count == len(requests) == 3
        assert [call.args[0] for call in provider_retry_sleep.await_args_list] == waits
        assert provider._structured_output is True
    finally:
        await provider.aclose()


@pytest.mark.parametrize("offset,aware", [(4, True), (-4, True), (4, False)])
async def test_http_date_waits_use_utc_and_do_not_delay_a_past_deadline(
    offset, aware, monkeypatch, provider_retry_sleep
):
    date = datetime.fromtimestamp(NOW + offset, UTC)
    if not aware:
        date = date.replace(tzinfo=None)
    header = format_datetime(date, usegmt=aware)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: NOW), raising=False)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            503, headers={"Retry-After": header}, json={"error": {"message": "busy"}}
        )

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError):
            await provider._call("報告を確認する")
        assert len(requests) == 3
        assert [call.args[0] for call in provider_retry_sleep.await_args_list] == [
            max(0, offset)
        ] * 2
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": "61"},
        {"retry-after-ms": "60001"},
        {"Retry-After": format_datetime(datetime.fromtimestamp(NOW + 61, UTC), usegmt=True)},
    ],
)
async def test_a_wait_beyond_the_retry_window_stops_instead_of_sending_early(
    headers, monkeypatch, provider_retry_sleep
):
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: NOW))
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers=headers, json={"error": {"message": "later"}})

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError) as error:
            await provider._call("報告を確認する")
        assert error.value.status_code == 429
        assert provider.call_count == len(requests) == 1
        provider_retry_sleep.assert_not_awaited()
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "status,hint,count", [(503, "false", 1), (400, "true", 3), (503, "unknown", 3)]
)
async def test_explicit_retry_directives_take_precedence_over_default_status_policy(
    status, hint, count, provider_retry_sleep
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"x-should-retry": hint}, json={"error": {"message": "gateway result"}}
        )

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError):
            await provider._call("報告を確認する")
        assert provider.call_count == len(requests) == count
        assert provider_retry_sleep.await_count == count - 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "headers", [{"Retry-After": "4"}, {"x-should-retry": "false"}, {"Retry-After": "61"}]
)
async def test_schema_fallback_also_obeys_waits_and_no_retry_directives(
    headers, provider_retry_sleep
):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                headers=headers,
                json={"error": {"message": "response_format is not supported"}},
            )
        return completion('{"accepted":true}')

    provider = await llm_with_transport(handle)
    try:
        if headers == {"Retry-After": "4"}:
            assert await provider._call("報告を確認する", SCHEMA) == {"accepted": True}
            assert len(requests) == 2
            provider_retry_sleep.assert_awaited_once_with(4)
        else:
            with pytest.raises(APIStatusError):
                await provider._call("報告を確認する", SCHEMA)
            assert len(requests) == 1
            provider_retry_sleep.assert_not_awaited()
        assert provider._structured_output is False
    finally:
        await provider.aclose()


@pytest.mark.parametrize("stop", ["deadline", "operator"])
async def test_waiting_for_upstream_never_blocks_stop_or_task_deadline(tmp_path, monkeypatch, stop):
    entered = asyncio.Event()
    requests = []

    async def sleep(delay):
        entered.set()
        await asyncio.sleep(delay)

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep})))

    def handle(request):
        requests.append(request)
        return httpx.Response(
            429, headers={"Retry-After": "30"}, json={"error": {"message": "busy"}}
        )

    provider = await llm_with_transport(handle)
    display = FakeDisplayBackend(32, 32)
    store = TaskStore(tmp_path)
    limits = ExecutionLimits(max_seconds=0.15 if stop == "deadline" else 5)
    session = TaskSession(llm=provider, display=display, store=store, limits=limits, decision=None)
    task = asyncio.create_task(session.run("報告を保存する"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stop == "operator":
            session.stop()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is False
        assert len(requests) == provider.call_count == 1
        record = TaskStore(tmp_path).load(session.id)
        assert record.state == "failed" and record.actions == []
        if stop == "deadline":
            assert record.stop_reason == "duration_limit"
        assert not display.is_connected and provider._client.is_closed()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
