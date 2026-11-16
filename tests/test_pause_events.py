import asyncio

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_pause_resume_broadcast_and_persist_without_loop_progress(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
    session.loop.start(Goal(description="test"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask(id="s", description="test")])
    monkeypatch.setattr(server, "_active_session", session)
    events = []
    session.on_state_change(lambda state, ctx: events.append(state.value))
    assert await server.pause_task() == {"status": "paused"}
    await asyncio.gather(*session._control_notifications)
    assert events == ["paused"]
    assert store.load(session.id).state == "paused"
    assert await server.resume_task() == {"status": "resumed"}
    await asyncio.gather(*session._control_notifications)
    assert events == ["paused", "executing"]
    assert store.load(session.id).state == "executing"
    assert not session._control_notifications
