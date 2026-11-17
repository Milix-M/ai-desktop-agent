"""HTTP handovers must serialize one environment without stalling other environments."""

import asyncio
import threading

import pytest
from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_control import VmController
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def environments(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    monkeypatch.delenv("VM_CONTAINER_NAME", raising=False)
    client = _FakeDocker()
    for service in ("vm", "desktop"):
        container = _FakeContainer(
            f"ai-desktop-agent-{service}-1",
            labels={"com.docker.compose.service": service},
            health="healthy",
        )
        client.containers.items.append(container)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path / "tasks"))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "_task_submissions", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: FakeDisplayBackend())
    made = []

    class WaitingLLM(MockLLMProvider):
        async def understand_instruction(self, *args, **kwargs):
            await asyncio.Event().wait()

    def create(display):
        session = TaskSession(llm=WaitingLLM(), display=display, store=server.get_store())
        made.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    return pool, client, made


async def stop_sessions(made):
    for session in made:
        session.stop()
    await asyncio.gather(*(s._task for s in made if s._task), return_exceptions=True)


@pytest.mark.parametrize("other_operation", ["stop", "task"])
async def test_slow_stop_keeps_another_environment_available(
    environments, monkeypatch, other_operation
):
    _, client, made = environments
    main = client.containers.get("ai-desktop-agent-vm-1")
    entered, release = threading.Event(), threading.Event()
    original = main.stop

    def stop(timeout):
        entered.set()
        assert release.wait(10)
        original(timeout)

    monkeypatch.setattr(main, "stop", stop)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = asyncio.create_task(api.post("/vms/vm/stop"))
        other = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            other = asyncio.create_task(
                api.post("/vms/desktop/stop")
                if other_operation == "stop"
                else api.post("/tasks", json={"instruction": "確認する", "vm_id": "desktop"})
            )
            _, pending = await asyncio.wait([other], timeout=1)
            assert not pending, "another VM's HTTP operation waited for this VM's stop"
            assert other.result().status_code == 200, other.result().text
            assert not first.done()
        finally:
            release.set()
            await asyncio.gather(first, *([other] if other else []), return_exceptions=True)
            await stop_sessions(made)


async def test_slow_display_connection_keeps_another_environment_available(
    environments, monkeypatch
):
    _, _, made = environments
    entered, release = threading.Event(), threading.Event()

    def connect(vm):
        if vm.id == "vm":
            entered.set()
            assert release.wait(10)
        return FakeDisplayBackend()

    monkeypatch.setattr(server, "_connect_vm_display", connect)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = asyncio.create_task(api.post("/tasks", json={"instruction": "待つ", "vm_id": "vm"}))
        other = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            other = asyncio.create_task(
                api.post("/tasks", json={"instruction": "確認する", "vm_id": "desktop"})
            )
            _, pending = await asyncio.wait([other], timeout=1)
            assert not pending, "another VM's task waited for this VM's display connection"
            assert other.result().status_code == 200, other.result().text
        finally:
            release.set()
            await asyncio.gather(first, *([other] if other else []), return_exceptions=True)
            await stop_sessions(made)


async def test_debug_restart_stops_the_explicit_environment_instead_of_the_default(
    environments, monkeypatch
):
    _, client, made = environments
    controller = VmController(container_name="ai-desktop-agent-desktop-1", client=client)
    monkeypatch.setattr(controller, "_qmp_status", lambda: None)
    monkeypatch.setattr(server, "_get_vm_controller", lambda: controller)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            for vm_id in ("vm", "desktop"):
                response = await api.post("/tasks", json={"instruction": "待つ", "vm_id": vm_id})
                assert response.status_code == 200, response.text
            response = await api.post("/vm/restart")
            assert response.status_code == 200, response.text
            vm, desktop = made
            assert vm.is_running, "debug restart stopped a task on a different environment"
            assert not desktop.is_running, "the task on the restarted environment kept running"
        finally:
            await stop_sessions(made)


@pytest.mark.parametrize("operation", ["stop", "delete", "restart"])
async def test_cancelled_operation_still_holds_its_environment_until_docker_finishes(
    environments, monkeypatch, operation
):
    _, client, made = environments
    main = client.containers.get("ai-desktop-agent-vm-1")
    entered, release = threading.Event(), threading.Event()
    method = "restart" if operation == "restart" else "stop"
    original = getattr(main, method)

    def blocked(timeout):
        entered.set()
        assert release.wait(10)
        original(timeout)

    monkeypatch.setattr(main, method, blocked)
    if operation == "restart":
        controller = VmController(client=client)
        monkeypatch.setattr(controller, "_qmp_status", lambda: None)
        monkeypatch.setattr(server, "_get_vm_controller", lambda: controller)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        if operation == "delete":
            first = asyncio.create_task(api.delete("/vms/vm"))
        else:
            first = asyncio.create_task(
                api.post("/vm/restart" if operation == "restart" else "/vms/vm/stop")
            )
        same = other = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(3):
                first.cancel()
                await asyncio.sleep(0)
            same = asyncio.create_task(api.post("/vms/vm/start"))
            other = asyncio.create_task(
                api.post("/tasks", json={"instruction": "確認する", "vm_id": "desktop"})
            )
            _, pending = await asyncio.wait([other], timeout=1)
            assert not pending, "another environment waited for cancellation cleanup"
            assert other.result().status_code == 200, other.result().text
            assert not same.done() and not first.done()
            assert not main.started, "same-environment start raced the unfinished Docker call"
        finally:
            release.set()
            await asyncio.gather(first, *(t for t in (same, other) if t), return_exceptions=True)
            await stop_sessions(made)
    assert first.cancelled()
    assert same.result().status_code == (404 if operation == "delete" else 200)
    assert not server._vm_operations._entries


async def test_default_task_revalidates_the_chosen_environment_after_waiting(
    environments, monkeypatch
):
    _, client, made = environments
    main = client.containers.get("ai-desktop-agent-desktop-1")
    entered, release = threading.Event(), threading.Event()
    chosen = asyncio.Event()
    original = main.stop
    choose = server._default_vm_id

    def stop(timeout):
        entered.set()
        assert release.wait(10)
        original(timeout)

    loop = asyncio.get_running_loop()

    def select():
        result = choose()
        loop.call_soon_threadsafe(chosen.set)
        return result

    monkeypatch.setattr(main, "stop", stop)
    monkeypatch.setattr(server, "_default_vm_id", select)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        stopping = asyncio.create_task(api.post("/vms/desktop/stop"))
        submission = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            submission = asyncio.create_task(api.post("/tasks", json={"instruction": "確認する"}))
            await asyncio.wait_for(chosen.wait(), 1)
            assert not submission.done()
        finally:
            release.set()
            await asyncio.gather(
                stopping, *([submission] if submission else []), return_exceptions=True
            )
            await stop_sessions(made)
    assert submission.result().status_code == 409
    assert not made, "a task acquired a display using stale readiness from before the stop"


async def test_cancelled_waiter_does_not_replace_the_environment_lock(environments):
    locks = server._vm_operations
    acquired = asyncio.Event()

    async def wait():
        async with locks.hold("vm"):
            acquired.set()

    async with locks.hold("vm"):
        abandoned = asyncio.create_task(wait())
        survivor = asyncio.create_task(wait())
        await asyncio.sleep(0)
        abandoned.cancel()
        await asyncio.gather(abandoned, return_exceptions=True)
        assert not acquired.is_set() and not survivor.done()
    await survivor
    assert acquired.is_set() and not locks._entries


async def test_replay_does_not_wait_for_an_unrelated_environment_operation(
    environments, monkeypatch
):
    _, client, made = environments
    entered, release = threading.Event(), threading.Event()
    main = client.containers.get("ai-desktop-agent-vm-1")
    original = main.stop

    def stop(timeout):
        entered.set()
        assert release.wait(10)
        original(timeout)

    monkeypatch.setattr(main, "stop", stop)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        body = {"instruction": "確認する", "vm_id": "desktop"}
        headers = {"Idempotency-Key": "same-submission-key-00001"}
        initial = await api.post("/tasks", json=body, headers=headers)
        assert initial.status_code == 200
        stopping = asyncio.create_task(api.post("/vms/vm/stop"))
        retry = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            retry = asyncio.create_task(api.post("/tasks", json=body, headers=headers))
            _, pending = await asyncio.wait([retry], timeout=1)
            assert not pending, "replaying an accepted task waited for another environment"
            assert retry.result().status_code == 200
            assert retry.result().json()["session_id"] == initial.json()["session_id"]
            assert retry.result().json()["replayed"] is True
        finally:
            release.set()
            await asyncio.gather(stopping, *([retry] if retry else []), return_exceptions=True)
            await stop_sessions(made)
    assert len(made) == 1 and not server._task_submissions._entries


async def test_interleaved_start_order_keeps_the_latest_task_active(environments, monkeypatch):
    _, _, made = environments
    entered, release = threading.Event(), threading.Event()
    store = server.get_store()
    original = store.reserve_session_order

    def reserve():
        order = original()
        if order == 1:
            entered.set()
            assert release.wait(10)
        return order

    monkeypatch.setattr(store, "reserve_session_order", reserve)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = asyncio.create_task(api.post("/tasks", json={"instruction": "待つ", "vm_id": "vm"}))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(
                api.post("/tasks", json={"instruction": "確認する", "vm_id": "desktop"})
            )
            _, pending = await asyncio.wait([second], timeout=1)
            assert not pending and second.result().status_code == 200
            release.set()
            assert (await first).status_code == 200
            latest = await api.get("/tasks/current")
            assert latest.json()["session_id"] == second.result().json()["session_id"]
            assert latest.json()["session_order"] == 2
            assert sorted(s.session_order for s in made) == [1, 2]
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            await stop_sessions(made)


async def test_cancelled_task_acceptance_holds_the_vm_until_its_initial_save_finishes(
    environments, monkeypatch
):
    _, _, made = environments
    entered, release = threading.Event(), threading.Event()
    store = server.get_store()
    original = store.save

    def save(record):
        if record.vm_id == "vm" and not entered.is_set():
            entered.set()
            assert release.wait(10)
        original(record)

    monkeypatch.setattr(store, "save", save)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = asyncio.create_task(api.post("/tasks", json={"instruction": "待つ", "vm_id": "vm"}))
        stopping = other = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            first.cancel()
            stopping = asyncio.create_task(api.post("/vms/vm/stop"))
            other = asyncio.create_task(
                api.post("/tasks", json={"instruction": "確認する", "vm_id": "desktop"})
            )
            _, pending = await asyncio.wait([other], timeout=1)
            assert not pending and other.result().status_code == 200
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done() and not stopping.done()
        finally:
            release.set()
            await asyncio.gather(
                first, *(t for t in (stopping, other) if t), return_exceptions=True
            )
            await stop_sessions(made)
    assert first.cancelled()
    assert stopping.result().status_code == 200
    assert not server._vm_operations._entries
