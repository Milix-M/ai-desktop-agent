import asyncio
from types import SimpleNamespace

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class WaitingLLM(MockLLMProvider):
    async def understand_instruction(self, *args, **kwargs):
        await asyncio.Event().wait()


async def test_interleaved_and_concurrent_submissions_keep_one_task_per_vm(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_task_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm_id: SimpleNamespace(id=vm_id))
    monkeypatch.setattr(server, "_connect_vm_display", lambda vm: FakeDisplayBackend())
    monkeypatch.setattr(
        server,
        "_create_session",
        lambda: TaskSession(llm=WaitingLLM(), display=FakeDisplayBackend()),
    )
    try:
        for vm_id in ["A", "B", "A"]:
            await server.create_task(server.CreateTaskRequest(instruction="test", vm_id=vm_id))
            await asyncio.sleep(0)
        running = [s.vm_id for s in server._sessions.values() if s.is_running]
        assert sorted(running) == ["A", "B"]
        await asyncio.gather(
            *(
                server.create_task(server.CreateTaskRequest(instruction="test", vm_id="A"))
                for _ in range(3)
            )
        )
        assert sum(s.vm_id == "A" and s.is_running for s in server._sessions.values()) == 1
    finally:
        sessions = list(server._sessions.values())
        for s in sessions:
            s.stop()
        await asyncio.gather(*(s._task for s in sessions), return_exceptions=True)
