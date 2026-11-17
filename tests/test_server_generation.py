import asyncio
import re

from fastapi.testclient import TestClient
from httpx import ASGITransport

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import Goal
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AUTH_HEADERS, AuthenticatedClient


async def test_rest_status_identifies_the_running_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.get("/tasks/current")).json()
        second = (await api.get("/tasks/current?vm_id=desktop")).json()
    assert re.fullmatch(r"[a-f0-9]{32}", first["server_id"])
    assert second["server_id"] == first["server_id"]


def test_websocket_and_rest_report_the_same_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    client = TestClient(server.app, headers=AUTH_HEADERS, client=("127.0.0.1", 50000))
    with client.websocket_connect("/ws") as ws:
        snapshot = ws.receive_json()
        status = client.get("/tasks/current").json()
        assert snapshot["server_id"] == status["server_id"]


async def test_all_lifecycle_events_identify_the_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    for name in ("_ws_state_cbs", "_ws_action_cbs", "_ws_error_cbs", "_ws_complete_cbs"):
        monkeypatch.setattr(server, name, [])
    payloads = []
    ready = asyncio.Event()

    class Viewer:
        async def accept(self):
            pass

        async def send_json(self, payload):
            payloads.append(payload)
            if payload["type"] == "snapshot":
                ready.set()

        async def receive(self):
            await asyncio.Event().wait()

    endpoint = asyncio.create_task(server.websocket_endpoint(Viewer()))
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend())
    session.loop.start(Goal(description="画面を確認する"))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        await server._fanout(
            server._ws_state_cbs, session, session.loop.state, session.loop.context
        )
        await server._fanout(
            server._ws_action_cbs, session, Action(ActionType.WAIT, {"seconds": 0}), True
        )
        await server._fanout(server._ws_error_cbs, session, "確認用のエラー")
        await server._fanout(server._ws_complete_cbs, session, False)
        async with asyncio.timeout(2):
            while len(payloads) < 5:
                await asyncio.sleep(0.01)
        assert {p["type"] for p in payloads} == {"snapshot", "state", "action", "error", "complete"}
        assert all(p["server_id"] == server._server_id for p in payloads)
    finally:
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)
        await session.aclose()
