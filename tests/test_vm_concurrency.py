"""Keep slow VM creation isolated while preserving destructive-operation ordering."""

import asyncio
import sys
import threading
import traceback

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import vm_pool
from ai_desktop_agent.server.vm_control import VmController
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.async_io import run_vm_call
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def slow_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.setenv("ALLOW_TCG_VM", "false")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    monkeypatch.delenv("VM_CONTAINER_NAME", raising=False)
    client = _FakeDocker()
    for service in ("vm", "desktop"):
        container = _FakeContainer(
            "ai-desktop-agent-" + service + "-1",
            labels={"com.docker.compose.service": service},
        )
        container.attrs["State"]["Running"] = True
        client.containers.items.append(container)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path))
    controller = VmController(client=client)
    monkeypatch.setattr(controller, "_qmp_status", lambda: None)
    entered, release = threading.Event(), threading.Event()
    identifiers = []

    def copy(vm_id, *, image=None):
        assert image is not None
        identifiers.append(vm_id)
        entered.set()
        assert release.wait(10), "test did not release the controlled disk copy"
        return str(tmp_path / vm_id / "disk.qcow2")

    monkeypatch.setattr(pool, "_create_overlay", copy)
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    monkeypatch.setattr(server, "_get_vm_controller", lambda: controller)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    yield pool, client, entered, release, identifiers
    release.set()


async def test_a_copy_does_not_block_status_list_or_another_environments_stop(slow_creation):
    _, _, entered, release, _ = slow_creation
    tasks = []
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        creation = asyncio.create_task(api.post("/vms", json={"kind": "qemu"}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            tasks = [
                asyncio.create_task(api.get("/vm/status")),
                asyncio.create_task(api.get("/vms")),
                asyncio.create_task(api.post("/vms/desktop/stop")),
            ]
            # Check progress before releasing the copy; this is not an API latency benchmark.
            _, pending = await asyncio.wait(tasks, timeout=2)
            if pending:
                stacks = "\n".join(
                    "".join(traceback.format_stack(frame))
                    for frame in sys._current_frames().values()
                )
                pytest.fail("a different environment is waiting for the disk copy\n" + stacks)
            assert all(task.result().status_code == 200 for task in tasks)
            assert not creation.done()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            created = await creation
    assert created.status_code == 200, created.text


async def test_concurrent_creators_still_enforce_the_shared_limit(slow_creation, monkeypatch):
    pool, client, entered, release, identifiers = slow_creation
    monkeypatch.setattr(vm_pool, "MAX_VMS", 1)
    first = asyncio.create_task(run_vm_call(pool.create_vm))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        another_pool = VmPool(client=client, state_file=pool._state_file)
        second = asyncio.create_task(run_vm_call(another_pool.create_vm))
        await asyncio.sleep(0.05)
        assert not second.done()
        assert len(identifiers) == 1
    finally:
        release.set()
        created = await first
        if second is not None:
            with pytest.raises(ValueError, match="上限"):
                await second
    assert pool.get_vm(created.id) is not None
    assert len([v for v in pool.list_vms() if v.managed]) == 1


@pytest.mark.parametrize("copy_fails", [False, True])
async def test_delete_waits_for_creation_or_its_cleanup(slow_creation, monkeypatch, copy_fails):
    pool, _, entered, release, identifiers = slow_creation
    if copy_fails:
        original = pool._create_overlay

        def fail(vm_id, *, image=None):
            original(vm_id, image=image)
            raise RuntimeError("controlled copy failure")

        monkeypatch.setattr(pool, "_create_overlay", fail)
    creation = asyncio.create_task(run_vm_call(pool.create_vm))
    deletion = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        deletion = asyncio.create_task(run_vm_call(pool.remove_vm, identifiers[0]))
        await asyncio.sleep(0.05)
        assert not deletion.done(), "deletion raced the disk copy or rollback"
    finally:
        release.set()
        if copy_fails:
            with pytest.raises(RuntimeError, match="controlled copy failure"):
                await creation
        else:
            await creation
        if deletion is not None:
            assert await deletion is (not copy_fails)
    assert pool.get_vm(identifiers[0]) is None
    assert pool._load_state(identifiers[0]) is None


@pytest.mark.parametrize("cancelled", [False, True])
async def test_debug_restart_and_pool_delete_share_the_same_environment_lock(
    slow_creation, monkeypatch, cancelled
):
    pool, client, _, _, _ = slow_creation
    controller = VmController(client=client)
    monkeypatch.setattr(controller, "_qmp_status", lambda: None)
    main = client.containers.get("ai-desktop-agent-vm-1")
    original = main.restart
    entered, release = threading.Event(), threading.Event()

    def restart(timeout):
        entered.set()
        assert release.wait(10)
        original(timeout)

    monkeypatch.setattr(main, "restart", restart)
    restarting = asyncio.create_task(run_vm_call(controller.restart))
    deletion = other_stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancelled:
            restarting.cancel()
        deletion = asyncio.create_task(run_vm_call(pool.remove_vm, "vm"))
        other_stop = asyncio.create_task(run_vm_call(pool.stop_vm, "desktop"))
        _, pending = await asyncio.wait([other_stop], timeout=2)
        assert not pending, "another environment was blocked by this restart"
        assert other_stop.result().status == "exited"
        assert not deletion.done()
        assert not restarting.done()
    finally:
        release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await restarting
        else:
            assert (await restarting).name == main.name
        if deletion is not None:
            assert await deletion is True
        if other_stop is not None:
            await other_stop
    assert pool.get_vm("vm") is None


@pytest.mark.parametrize("vm_id", ["desktop", "desk-explicit"])
async def test_explicit_debug_target_uses_the_pools_environment_lock(
    slow_creation, monkeypatch, vm_id
):
    pool, client, _, _, _ = slow_creation
    if vm_id == "desktop":
        main = client.containers.get("ai-desktop-agent-desktop-1")
    else:
        main = _FakeContainer(
            "explicit-desktop",
            labels={"ai-desktop-agent.vm-id": vm_id, "ai-desktop-agent.kind": "container"},
        )
        client.containers.items.append(main)
    controller = VmController(container_name=main.name, client=client)
    monkeypatch.setattr(controller, "_qmp_status", lambda: None)
    original = main.restart
    entered, release = threading.Event(), threading.Event()

    def restart(timeout):
        entered.set()
        assert release.wait(10)
        original(timeout)

    monkeypatch.setattr(main, "restart", restart)
    restarting = asyncio.create_task(run_vm_call(controller.restart))
    deletion = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        deletion = asyncio.create_task(run_vm_call(pool.remove_vm, vm_id))
        await asyncio.sleep(0.05)
        assert not deletion.done(), "explicit target bypassed the environment lock"
    finally:
        release.set()
        await restarting
        if deletion is not None:
            assert await deletion is True
    assert pool.get_vm(vm_id) is None
