"""Deletion cannot detach a registered task while its startup wrapper is queued."""

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


@pytest.mark.parametrize("keyed", [False, True], ids=["legacy", "retry-key"])
async def test_pending_start_refuses_deletion_then_remains_controllable_and_can_be_deleted(
    tmp_path, monkeypatch, keyed
):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm: SimpleNamespace(id=vm))
    monkeypatch.setattr(
        server,
        "_connect_vm_display",
        lambda _: FakeDisplayBackend(screen_width=96, screen_height=64),
    )
    made = []
    model_entered, model_finish = asyncio.Event(), asyncio.Event()
    decisions = []

    class WaitingModel(MockLLMProvider):
        async def decide_next_action(self, *args, **kwargs):
            decisions.append("decide")
            model_entered.set()
            await model_finish.wait()
            return await super().decide_next_action(*args, **kwargs)

    def create(display):
        session = TaskSession(llm=WaitingModel(), display=display, store=store, decision=None)
        made.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    queued, release_start = asyncio.Event(), asyncio.Event()

    def scheduling_observer(loop, coro, **kwargs):
        # Delay only through the event loop's public scheduling hook. The actual
        # session coroutine, state transitions and durable writes remain intact.
        if getattr(coro, "cr_code", None) is TaskSession.start_async.__code__:

            async def delayed_start():
                queued.set()
                await release_start.wait()
                return await coro

            scheduled = delayed_start()
        else:
            scheduled = coro
        if previous_factory is not None:
            return previous_factory(loop, scheduled, **kwargs)
        return asyncio.Task(scheduled, loop=loop, **kwargs)

    loop.set_task_factory(scheduling_observer)
    request = None
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            payload = {"instruction": "保存済み文書を確認する", "vm_id": "desktop"}
            headers = {"Idempotency-Key": "startup-delete-retry-0123456789"} if keyed else {}
            request = asyncio.create_task(api.post("/tasks", json=payload, headers=headers))
            await asyncio.wait_for(queued.wait(), 3)
            pending = await api.get("/tasks/current")
            task_id = pending.json()["session_id"]
            assert pending.status_code == 200 and task_id
            assert pending.json()["state"] == "idle" and store.load(task_id) is None
            deletion = await api.delete("/tasks/" + task_id)
            assert deletion.status_code == 409, deletion.text
            assert deletion.headers["Retry-After"] == "1" and "開始" in deletion.json()["detail"]
            assert store.load(task_id) is None and not decisions
            assert (await api.get("/tasks/current")).json()["session_id"] == task_id

            release_start.set()
            started = await asyncio.wait_for(asyncio.shield(request), 3)
            assert started.status_code == 200 and started.json()["session_id"] == task_id
            await asyncio.wait_for(model_entered.wait(), 3)
            current = await api.get("/tasks/current", params={"vm_id": "desktop"})
            assert current.json()["session_id"] == task_id and current.json()["is_running"]
            control = await api.post(
                "/tasks/current/pause", params={"vm_id": "desktop", "session_id": task_id}
            )
            assert control.status_code == 200 and control.json()["status"] == "paused"
            removed = await api.delete("/tasks/" + task_id)
            assert removed.status_code == 200, removed.text
            assert not made[0].is_running and not made[0].display.is_connected
            assert store.load(task_id) is None
            assert (await api.get("/tasks/" + task_id)).status_code == 404
            assert (await api.get("/tasks/current")).json()["session_id"] is None
            if keyed:
                replay = await api.post("/tasks", json=payload, headers=headers)
                assert replay.status_code == 410, replay.text
            assert len(made) == 1 and decisions == ["decide"]
            assert not made[0].display.clicks and not made[0].display.text_inputs
    finally:
        release_start.set()
        model_finish.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        for session in made:
            session.stop()
        await asyncio.gather(*(session.aclose() for session in made))
        loop.set_task_factory(previous_factory)
