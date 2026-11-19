"""Use real backend startup and task APIs to arbitrate Android setup ownership."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import android_provision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_pool import VmInfo, VmInstance
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.http import AuthenticatedClient


class HeldModel(MockLLMProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide_next_action(self, *args, **kwargs):
        history = kwargs["action_history"]
        if not history:
            self.entered.set()
            await self.release.wait()
            return ActionDecision(
                Action(ActionType.MOUSE_MOVE, {"x": 8, "y": 9}),
                "inspect the application",
                1.0,
                "move to the visible target",
            )
        return await super().decide_next_action(*args, **kwargs)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("old-running", "unfinished report", "executing"))
    infos = {
        vm_id: VmInfo(vm_id, vm_id, "running", health="healthy", kind="android", vnc_host=vm_id)
        for vm_id in ["and-first", "and-other"]
    }
    pool = Mock()
    pool.list_vms.side_effect = lambda: list(infos.values())
    pool.get_vm.side_effect = infos.get
    pool.get_instance.side_effect = lambda vm_id: VmInstance(
        info=infos[vm_id], container_id="container-" + vm_id
    )
    pool.instance_id.side_effect = lambda vm_id: "container-" + vm_id
    services, sessions, attempts = [], [], []

    class ObservedSetup(android_provision.AndroidProvisioner):
        complete = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            services.append(self)

        def provision_vm(self, host, *, before_execute, cancelled):
            assert before_execute() and not cancelled()
            attempts.append(host)
            return self.complete

    def create(display):
        session = TaskSession(llm=HeldModel(), display=display, store=store, decision=None)
        sessions.append(session)
        return session

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANDROID_PROVISION", "0")  # Run each actual ensure cycle explicitly.
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_vm_creations", None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_snapshot_readers", {})
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    for name in ["state", "action", "error", "complete"]:
        monkeypatch.setattr(server, "_ws_" + name + "_cbs", [])
    monkeypatch.setattr(server, "_create_session", create)
    monkeypatch.setattr(server, "_connect_vm_display", lambda vm: FakeDisplayBackend())
    monkeypatch.setattr(android_provision, "AndroidProvisioner", ObservedSetup)
    monkeypatch.setattr(VNCClient, "shutdown_runtime", Mock())
    return SimpleNamespace(
        store=store,
        pool=pool,
        services=services,
        sessions=sessions,
        attempts=attempts,
        setup_file=tmp_path / "android-provision.json",
    )


@pytest.mark.parametrize("finish", ["stop", "complete"])
async def test_setup_skips_only_live_tasks_and_resumes_after_their_real_lifecycle(backend, finish):
    async with server.lifespan(server.app):
        assert backend.store.load("old-running").state == "interrupted"
        setup = backend.services[0]
        await asyncio.to_thread(setup.ensure)
        assert backend.attempts == ["and-first", "and-other"]
        assert not backend.setup_file.exists()
        backend.attempts.clear()

        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as api:

            async def start(vm_id):
                response = await api.post(
                    "/tasks", json={"instruction": "inspect report", "vm_id": vm_id}
                )
                assert response.status_code == 200, response.text
                session = backend.sessions[-1]
                await asyncio.wait_for(session.llm.entered.wait(), 2)
                assert response.json()["session_id"] == session.id and session.is_running
                return session

            first = await start("and-first")
            await asyncio.to_thread(setup.ensure)
            assert backend.attempts == ["and-other"]
            backend.attempts.clear()
            other = await start("and-other")
            await asyncio.to_thread(setup.ensure)
            assert backend.attempts == [] and not backend.setup_file.exists()

            async def finish_session(session):
                if finish == "stop":
                    response = await api.post(
                        "/tasks/current/stop",
                        params={"vm_id": session.vm_id, "session_id": session.id},
                    )
                    assert response.status_code == 200 and response.json()["status"] == "stopped"
                    result = await asyncio.gather(session._task, return_exceptions=True)
                    assert isinstance(result[0], asyncio.CancelledError)
                else:
                    session.llm.release.set()
                    assert await asyncio.wait_for(asyncio.shield(session._task), 3) is True
                assert not session.is_running and not session.display.is_connected
                assert backend.store.load(session.id).state == (
                    "failed" if finish == "stop" else "completed"
                )

            await finish_session(first)
            assert other.is_running
            await asyncio.to_thread(setup.ensure)
            assert backend.attempts == ["and-first"]
            backend.attempts.clear()
            await finish_session(other)
            setup.complete = True
            await asyncio.to_thread(setup.ensure)
            assert backend.attempts == ["and-first", "and-other"]
            state = json.loads(backend.setup_file.read_text())
            assert set(state) == {"and-first", "and-other"}
            assert all(record["done"] for record in state.values())
            assert {record["data_id"] for record in state.values()} == {
                "container:container-and-first",
                "container:container-and-other",
            }
            backend.attempts.clear()
            await asyncio.to_thread(setup.ensure)
            assert backend.attempts == []
    backend.pool.close.assert_called_once()
    assert server._android_provisioner is None and not server._sessions
