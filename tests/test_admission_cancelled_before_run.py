"""Deleting an admitted run must wait for the real pre-start cancellation audit."""

import asyncio
import threading
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


async def test_delete_drains_the_cancellation_audit_before_removing_a_run_cancelled_before_start(
    tmp_path, monkeypatch
):
    saving, release = threading.Event(), threading.Event()
    saved_records = []

    class DelayedCancellationStore(TaskStore):
        def save(self, record):
            if record.state == "failed" and not saving.is_set():
                saving.set()
                assert release.wait(5)
            result = super().save(record)
            saved_records.append(asdict(record))
            return result

    store = DelayedCancellationStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm: SimpleNamespace(id=vm))
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: FakeDisplayBackend(96, 64))
    made = []
    model = MockLLMProvider()
    model.understand_instruction = AsyncMock(wraps=model.understand_instruction)
    model.aclose = AsyncMock()

    def create(display):
        session = TaskSession(llm=model, display=display, store=store, decision=None)
        made.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()

    def cancel_new_run(loop, coroutine, **kwargs):
        task = (
            previous_factory(loop, coroutine, **kwargs)
            if previous_factory is not None
            else asyncio.Task(coroutine, loop=loop, **kwargs)
        )
        if getattr(coroutine, "cr_code", None) is TaskSession.run.__code__:
            task.cancel()
        return task

    loop.set_task_factory(cancel_new_run)
    requests = []
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            headers = {"Idempotency-Key": "cancel-before-run-audit-0123456789"}
            payload = {"instruction": "保存済み文書を確認する", "vm_id": "desktop"}
            admission = asyncio.create_task(api.post("/tasks", json=payload, headers=headers))
            requests.append(admission)
            assert await asyncio.to_thread(saving.wait, 2)
            session = made[0]
            assert session._task.cancelled() and not admission.done()
            deleting = asyncio.create_task(api.delete("/tasks/" + session.id))
            requests.append(deleting)
            # A health round trip gives the queued deletion time to reach its
            # cleanup wait while the independent durable write is still held.
            assert (await api.get("/health")).status_code == 200
            await asyncio.sleep(0)
            assert not deleting.done()
            assert session.id in server._sessions and store.load(session.id) is None
            release.set()
            accepted, deleted = await asyncio.wait_for(asyncio.gather(*requests), 3)
            assert accepted.status_code == deleted.status_code == 200
            assert accepted.json()["state"] == "failed"
            assert saved_records[-1]["stop_reason"] == "cancelled"
            assert saved_records[-1]["state"] == "failed"
            assert saved_records[-1]["success"] is False
            assert store.load(session.id) is None and store.list(limit=None) == []
            assert session.id not in server._sessions and server._active_session is None
            assert not session.is_running and not session.display.is_connected
            model.understand_instruction.assert_not_awaited()
            model.aclose.assert_awaited_once()
            replay = await api.post("/tasks", json=payload, headers=headers)
            assert replay.status_code == 410 and len(made) == 1
    finally:
        release.set()
        loop.set_task_factory(previous_factory)
        await asyncio.gather(*requests, return_exceptions=True)
        for session in made:
            await session.aclose()
