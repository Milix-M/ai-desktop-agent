"""An independent durable claim must defer HTTP admission without consuming its key."""

import asyncio
from types import SimpleNamespace

from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.submissions import fingerprint, key_digest
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


async def test_an_independent_store_claim_returns_retry_after_then_allows_exactly_one_task(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path)
    independent = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm: SimpleNamespace(id=vm))
    connections, sessions = [], []
    completed = asyncio.Event()

    def connect(vm):
        connections.append(vm.id)
        return FakeDisplayBackend(screen_width=96, screen_height=64)

    def create(display):
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)
        session.on_complete(lambda _: completed.set())
        sessions.append(session)
        return session

    monkeypatch.setattr(server, "_connect_vm_display", connect)
    monkeypatch.setattr(server, "_create_session", create)
    key = "independent-store-pending-0123456789"
    payload = {"instruction": "文書を確認する", "vm_id": "desktop"}
    request = server.CreateTaskRequest(**payload)
    claim = independent.claim_submission(key_digest(key), fingerprint(request.model_dump()))
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            headers = {"Idempotency-Key": key}
            pending = await asyncio.wait_for(api.post("/tasks", json=payload, headers=headers), 2)
            assert pending.status_code == 409 and pending.headers["retry-after"] == "1"
            assert "受付中" in pending.json()["detail"]
            assert connections == sessions == [] and store.list(limit=None) == []
            assert server._sessions == {} and server._active_session is None

            claim.close()
            accepted = await api.post("/tasks", json=payload, headers=headers)
            assert accepted.status_code == 200, accepted.text
            await asyncio.wait_for(completed.wait(), 3)
            await sessions[0].aclose()
            record = store.load(accepted.json()["session_id"])
            assert record.success is True and record.state == "completed"
            replay = await api.post("/tasks", json=payload, headers=headers)
            assert replay.status_code == 200 and replay.json()["replayed"] is True
            assert replay.json()["session_id"] == record.id
            assert connections == ["desktop"] and len(sessions) == 1
            assert len(store.list(limit=None)) == 1
            assert not sessions[0].display.is_connected
    finally:
        claim.close()
        for session in sessions:
            session.stop()
            if session._task is not None:
                await asyncio.gather(session._task, return_exceptions=True)
            await session.aclose()
