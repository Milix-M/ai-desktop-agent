import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class WaitingLLM(MockLLMProvider):
    async def understand_instruction(self, *args, **kwargs):
        await asyncio.Event().wait()


@pytest.mark.parametrize("latest", [True, False])
async def test_deleted_running_task_stays_deleted(tmp_path, monkeypatch, latest):
    store = TaskStore(tmp_path)
    session = TaskSession(llm=WaitingLLM(), display=FakeDisplayBackend(), store=store)
    other = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend())
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {session.id: session})
    monkeypatch.setattr(server, "_active_session", session if latest else other)
    await session.start_async("test")
    await asyncio.sleep(0)
    assert store.load(session.id) is not None
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.delete(f"/tasks/{session.id}")
        assert response.status_code == 200
        await asyncio.sleep(0)
        assert session._task.done()
        assert store.load(session.id) is None
        assert session.id not in server._sessions
        assert (await client.get(f"/tasks/{session.id}")).status_code == 404
        assert server._active_session is (None if latest else other)
