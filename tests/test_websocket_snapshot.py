from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AUTH_HEADERS
from tests.http import AuthenticatedClient as AsyncClient


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    for name in ("_ws_state_cbs", "_ws_action_cbs", "_ws_error_cbs", "_ws_complete_cbs"):
        monkeypatch.setattr(server, name, [])
    return store


def test_reconnect_reports_tasks_that_finished_while_offline(store):
    store.save(TaskRecord("old-a", "保存する", "executing", vm_id="A"))
    store.save(TaskRecord("done-b", "入力する", "completed", success=True, vm_id="B"))
    client = TestClient(server.app, headers=AUTH_HEADERS, client=("127.0.0.1", 50000))
    with client.websocket_connect("/ws") as ws:
        snapshot = ws.receive_json()
        states = {task["vm_id"]: task["state"] for task in snapshot["tasks"]}
        assert snapshot["type"] == "snapshot"
        assert states == {"A": "executing", "B": "completed"}
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}
    store.save(TaskRecord("old-a", "保存する", "failed", success=False, vm_id="A"))
    with client.websocket_connect("/ws") as ws:
        states = {task["vm_id"]: task["state"] for task in ws.receive_json()["tasks"]}
        assert states["A"] == "failed"
    assert not server._ws_state_cbs
    assert not server._ws_action_cbs


async def test_snapshot_includes_all_running_vms_and_overrides_stale_storage(store, monkeypatch):
    store.save(TaskRecord("old", "保存する", "completed", success=True, vm_id="A"))
    sessions = []
    for vm_id in ("A", "B"):
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), vm_id=vm_id)
        session.loop.start(Goal(description="保存する"))
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask("s", "保存する")])
        session._task = SimpleNamespace(done=lambda: False)
        sessions.append(session)
    monkeypatch.setattr(server, "_sessions", {s.id: s for s in sessions})
    monkeypatch.setattr(server, "_active_session", sessions[-1])
    snapshot = await server._task_snapshot()
    assert {s.vm_id for s in snapshot} == {"A", "B"}
    assert all(s.is_running and s.state == "executing" for s in snapshot)
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.get("/tasks/current?vm_id=A")
        assert response.json()["session_id"] == sessions[0].id
        response = await client.get("/tasks/current?vm_id=missing")
        assert response.json()["session_id"] is None
        assert response.json()["vm_id"] == "missing"


async def test_restored_task_includes_vm_id(store):
    store.save(TaskRecord("done", "保存する", "completed", success=True, vm_id="desktop"))
    result = await server.get_current_task()
    assert result.vm_id == "desktop"


async def test_a_slow_viewer_cannot_block_task_events(store, monkeypatch):
    import asyncio

    from fastapi import WebSocketDisconnect

    started = asyncio.Event()

    class SlowViewer:
        async def accept(self):
            pass

        async def send_json(self, payload):
            started.set()
            await asyncio.Event().wait()

        async def receive(self):
            await asyncio.Event().wait()
            raise WebSocketDisconnect()

    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), vm_id="A")
    session.loop.start(Goal(description="保存する"))
    endpoint = asyncio.create_task(server.websocket_endpoint(SlowViewer()))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(
            server._fanout(server._ws_state_cbs, session, session.loop.state, session.loop.context),
            timeout=0.2,
        )
    finally:
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)
    assert not server._ws_state_cbs
