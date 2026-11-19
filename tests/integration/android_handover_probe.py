"""Run inside the backend image; only model decisions and ADB effects are controlled."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import docker
import httpx

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskRecord


class HeldModel(MockLLMProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide_next_action(self, *args, **kwargs):
        if not kwargs["action_history"]:
            self.entered.set()
            await self.release.wait()
            return ActionDecision(
                Action(ActionType.MOUSE_MOVE, {"x": 8, "y": 9}),
                "inspect the application",
                1.0,
                "move to the visible target",
            )
        return await super().decide_next_action(*args, **kwargs)


class ObservedSetup(AndroidProvisioner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        services.append(self)

    def provision_vm(self, host, *, before_execute, cancelled):
        assert before_execute() and not cancelled()
        guest = client.containers.get(host)
        result = guest.exec_run(
            ["sh", "-c", "printf 'setup\\n' >> /tmp/android-handover-setup-log"]
        )
        assert result.exit_code == 0, result.output
        attempts.append(guest.labels["ai-desktop-agent.vm-id"])
        return complete


def create_session(display):
    session = TaskSession(llm=HeldModel(), display=display, store=server.get_store(), decision=None)
    sessions.append(session)
    return session


async def exercise():
    global complete
    store = server.get_store()
    store.save(TaskRecord("old-running", "unfinished report", "executing"))
    async with server.lifespan(server.app):
        assert store.load("old-running").state == "interrupted"
        setup = services[0]

        async def cycle(phase, expected):
            attempts.clear()
            await asyncio.to_thread(setup.ensure)
            assert attempts == expected, (phase, attempts, expected)
            proof["phases"][phase] = list(attempts)

        await cycle("initial", ["and-first", "and-other"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + os.environ["AGENT_ACCESS_TOKEN"]},
        ) as api:

            async def start(vm_id):
                response = await api.post(
                    "/tasks", json={"instruction": "inspect report", "vm_id": vm_id}
                )
                assert response.status_code == 200, response.text
                session = sessions[-1]
                assert response.json()["session_id"] == session.id
                await asyncio.wait_for(session.llm.entered.wait(), 10)
                assert session.is_running
                await asyncio.to_thread(session.display.mouse_move, 40, 40)
                return session

            first = await start("and-first")
            await cycle("first-busy", ["and-other"])
            other = await start("and-other")
            await cycle("both-busy", [])

            async def finish(session):
                if MODE == "stop":
                    response = await api.post(
                        "/tasks/current/stop",
                        params={"vm_id": session.vm_id, "session_id": session.id},
                    )
                    assert response.status_code == 200 and response.json()["status"] == "stopped"
                    result = await asyncio.gather(session._task, return_exceptions=True)
                    assert isinstance(result[0], asyncio.CancelledError)
                else:
                    session.llm.release.set()
                    assert await asyncio.wait_for(asyncio.shield(session._task), 10) is True
                assert not session.is_running and not session.display.is_connected
                assert store.load(session.id).state == ("failed" if MODE == "stop" else "completed")

            await finish(first)
            assert other.is_running
            await cycle("first-finished", ["and-first"])
            await finish(other)
            complete = True
            await cycle("all-finished", ["and-first", "and-other"])
            state = json.loads(
                (Path(os.environ["DATA_DIR"]) / "android-provision.json").read_text()
            )
            assert set(state) == {"and-first", "and-other"}
            assert all(record["done"] for record in state.values())
            assert {key: record["data_id"] for key, record in state.items()} == {
                vm_id: "container:" + server.get_pool().instance_id(vm_id) for vm_id in state
            }
            proof["state"] = state
            proof["sessions"] = [
                store.load(session.id).to_dict(include_actions=False) for session in sessions
            ]
            await cycle("already-done", [])
    assert server._android_provisioner is None and not server._sessions
    proof["closed"] = all(
        not session.is_running and not session.display.is_connected for session in sessions
    )


if __name__ == "__main__":
    MODE = sys.argv[1]
    assert MODE in {"stop", "complete"}
    assert os.environ.get("COMPOSE_PROJECT_NAME", "").startswith("android-task-handover-")
    assert os.environ.get("DATA_DIR") == "/tmp/handover-data"
    client = docker.from_env(timeout=15)
    sessions, services, attempts = [], [], []
    complete = False
    proof = {"mode": MODE, "phases": {}}
    try:
        with (
            patch("ai_desktop_agent.server.android_provision.AndroidProvisioner", ObservedSetup),
            patch.object(server, "_create_session", create_session),
        ):
            asyncio.run(exercise())
    finally:
        client.close()
    print(json.dumps(proof), flush=True)
