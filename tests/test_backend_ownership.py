"""A rejected backend must not modify the running backend's durable state."""

import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_creations import CreationStore
from ai_desktop_agent.vm.vnc_client import VNCClient


@pytest.fixture
def backend(tmp_path, monkeypatch):
    store, pool = TaskStore(tmp_path), Mock()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_vm_creations", None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_server_id", server._server_id)
    monkeypatch.setattr(AndroidProvisioner, "start", AsyncMock())
    monkeypatch.setattr(AndroidProvisioner, "stop", AsyncMock())
    monkeypatch.setattr(VNCClient, "shutdown_runtime", Mock())
    return store, pool, CreationStore(tmp_path / "vm-creations")


async def test_duplicate_backend_does_not_interrupt_history_or_touch_environments(backend):
    store, pool, creations = backend
    store.save(TaskRecord(id="still-running", instruction="save the report", state="executing"))
    original = (store._dir / "still-running.json").read_bytes()
    identity = server._server_id
    owner = creations.acquire_owner()
    try:
        with pytest.raises(DockerUnavailableError):
            async with server.lifespan(server.app):
                pytest.fail("a second backend started")
        assert (store._dir / "still-running.json").read_bytes() == original
        assert server._server_id == identity
        pool.retire_legacy_relays.assert_not_called()
        pool.isolate_environments.assert_not_called()
        AndroidProvisioner.start.assert_not_awaited()
        AndroidProvisioner.stop.assert_not_awaited()
        VNCClient.shutdown_runtime.assert_not_called()
    finally:
        owner.close()


@pytest.mark.parametrize(
    "phase", ["auth", "redact", "interrupt", "retire", "isolate", "recover", "provision"]
)
async def test_startup_failure_releases_ownership(backend, monkeypatch, phase):
    store, pool, creations = backend
    service = server.get_vm_creations()
    failure = RuntimeError("controlled startup failure")
    targets = {
        "auth": (server, "get_access"),
        "redact": (store, "redact_existing"),
        "interrupt": (store, "mark_interrupted"),
        "retire": (pool, "retire_legacy_relays"),
        "isolate": (pool, "isolate_environments"),
        "recover": (service.store, "incomplete"),
        "provision": (AndroidProvisioner, "start"),
    }
    owner, name = targets[phase]
    factory = AsyncMock if phase == "provision" else Mock
    monkeypatch.setattr(owner, name, factory(side_effect=failure))
    with pytest.raises(RuntimeError, match="controlled startup failure"):
        async with server.lifespan(server.app):
            pytest.fail("failed startup became ready")
    assert server._vm_creations is None
    assert service._owner is None
    creations.acquire_owner().close()


@pytest.mark.parametrize("phase", ["acquire", "redact", "interrupt"])
async def test_cancelled_startup_drains_its_write_before_releasing_ownership(
    backend, monkeypatch, phase
):
    store, _, creations = backend
    service = server.get_vm_creations()
    entered, release = threading.Event(), threading.Event()
    target, name = (
        (service.store, "acquire_owner")
        if phase == "acquire"
        else (store, "redact_existing" if phase == "redact" else "mark_interrupted")
    )
    original = getattr(target, name)

    def blocked():
        result = original()
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(target, name, blocked)

    async def boot():
        async with server.lifespan(server.app):
            pytest.fail("cancelled startup became ready")

    startup = asyncio.create_task(boot())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            startup.cancel()
            await asyncio.sleep(0)
        assert not startup.done()
        with pytest.raises(DockerUnavailableError):
            creations.acquire_owner()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
    assert server._vm_creations is None
    assert service._owner is None
    creations.acquire_owner().close()


@pytest.mark.parametrize("recovery_failure", [False, True])
async def test_shutdown_keeps_ownership_until_the_vnc_runtime_has_finished(
    backend, monkeypatch, recovery_failure
):
    _, _, creations = backend
    entered, release = threading.Event(), threading.Event()

    def close_runtime():
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(VNCClient, "shutdown_runtime", close_runtime)
    lifecycle = server.lifespan(server.app)
    service = server.get_vm_creations()
    if recovery_failure:
        monkeypatch.setattr(
            service.store,
            "incomplete",
            Mock(side_effect=RuntimeError("controlled recovery failure")),
        )
        shutdown = asyncio.create_task(lifecycle.__aenter__())
    else:
        await lifecycle.__aenter__()
        shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            shutdown.cancel()
            await asyncio.sleep(0)
        assert not shutdown.done()
        with pytest.raises(DockerUnavailableError):
            creations.acquire_owner()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    assert service._owner is None
    assert server._vm_creations is None
    creations.acquire_owner().close()


async def test_concurrent_ownership_requests_and_start_use_one_descriptor(backend, monkeypatch):
    service = server.get_vm_creations()
    acquire = Mock(wraps=service.store.acquire_owner)
    monkeypatch.setattr(service.store, "acquire_owner", acquire)
    try:
        await asyncio.gather(*(service.acquire_ownership() for _ in range(5)))
        await service.start()
        acquire.assert_called_once()
    finally:
        await service.close()


async def test_owner_contention_can_be_retried_after_the_other_backend_stops(backend):
    _, _, creations = backend
    service = server.get_vm_creations()
    owner = creations.acquire_owner()
    try:
        with pytest.raises(DockerUnavailableError):
            await service.start()
    finally:
        owner.close()
    try:
        await service.start()
        with pytest.raises(DockerUnavailableError):
            creations.acquire_owner()
    finally:
        await service.close()
    creations.acquire_owner().close()
