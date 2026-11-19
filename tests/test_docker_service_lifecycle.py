"""Release service-owned pools after their final worker or rollback completes."""

import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import docker
import pytest

from ai_desktop_agent.server import __main__ as entrypoint
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import vm_pool, workspace_migration
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.test_vm_pool import _FakeDocker


@pytest.fixture
def connection(tmp_path, monkeypatch):
    client = _FakeDocker()
    client.ping, client.close = Mock(), Mock()
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    pool = VmPool(state_file=tmp_path / "vms.json")
    try:
        yield pool, client
    finally:
        pool.close()


@pytest.mark.parametrize("fails", [False, True])
async def test_backend_lifecycle_closes_the_shared_pool_after_startup_or_failure(
    connection, tmp_path, monkeypatch, fails
):
    pool, client = connection
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_vm_creations", None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(AndroidProvisioner, "start", AsyncMock())
    monkeypatch.setattr(AndroidProvisioner, "stop", AsyncMock())
    monkeypatch.setattr(VNCClient, "shutdown_runtime", Mock())
    if fails:

        def isolate():
            pool._docker()
            raise DockerUnavailableError("isolation failed")

        monkeypatch.setattr(pool, "isolate_environments", isolate)
        with pytest.raises(DockerUnavailableError, match="isolation failed"):
            async with server.lifespan(server.app):
                pytest.fail("startup failed")
    else:
        async with server.lifespan(server.app):
            assert pool._docker() is client
            client.close.assert_not_called()
    client.close.assert_called_once()
    assert server._pool is None and server._vm_creations is None


def test_container_bootstrap_closes_discovery_before_starting_the_server(connection, monkeypatch):
    pool, client = connection
    monkeypatch.setattr(entrypoint, "VmPool", lambda: pool)

    def serve(*args):
        client.close.assert_called_once()

    run = Mock(side_effect=serve)
    monkeypatch.setattr(entrypoint, "run_server", run)
    entrypoint.main()
    run.assert_called_once()


@pytest.mark.parametrize("provided", [False, True])
async def test_standalone_android_provisioner_releases_only_its_own_pool(
    connection, monkeypatch, provided
):
    pool, client = connection
    monkeypatch.setattr(vm_pool, "VmPool", lambda: pool)
    provisioner = AndroidProvisioner(pool=pool if provided else None)
    assert provisioner._pool_or_create()._docker() is client
    await provisioner.stop()
    await provisioner.stop()
    assert client.close.call_count == (0 if provided else 1)


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_workspace_transaction_keeps_its_connection_through_rollback_then_closes_owned_pool(
    connection, tmp_path, monkeypatch, provided, fails
):
    pool, client = connection
    monkeypatch.setattr(workspace_migration, "VmPool", lambda: pool)
    upgrade = workspace_migration.WorkspaceUpgrade(pool if provided else None, tmp_path)
    rollback = Mock(side_effect=lambda: client.close.assert_not_called())
    monkeypatch.setattr(upgrade, "_rollback", rollback)
    if fails:
        with pytest.raises(RuntimeError, match="compose failed"), upgrade.transaction():
            raise RuntimeError("compose failed")
        rollback.assert_called_once()
    else:
        with upgrade.transaction():
            client.close.assert_not_called()
    assert client.close.call_count == (0 if provided else 1)


def test_workspace_constructor_failure_closes_its_new_pool(connection, tmp_path, monkeypatch):
    pool, client = connection
    monkeypatch.setattr(workspace_migration, "VmPool", lambda: pool)
    monkeypatch.setattr(pool, "_project_name", Mock(side_effect=RuntimeError("inspect failed")))
    with pytest.raises(RuntimeError, match="inspect failed"):
        workspace_migration.WorkspaceUpgrade(root=tmp_path)
    client.close.assert_called_once()


async def test_cancelled_shutdown_finishes_a_running_creation_before_closing_its_connection(
    connection, tmp_path, monkeypatch
):
    pool, client = connection
    entered, release = threading.Event(), threading.Event()
    stopping = asyncio.Event()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_vm_creations", None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(AndroidProvisioner, "start", AsyncMock())
    monkeypatch.setattr(AndroidProvisioner, "stop", AsyncMock(side_effect=stopping.set))
    monkeypatch.setattr(VNCClient, "shutdown_runtime", Mock())
    create = pool.create_vm

    def blocked(**kwargs):
        assert pool._docker() is client
        entered.set()
        assert release.wait(3)
        client.close.assert_not_called()
        return create(**kwargs)

    monkeypatch.setattr(pool, "create_vm", blocked)
    lifecycle = server.lifespan(server.app)
    await lifecycle.__aenter__()
    service = server.get_vm_creations()
    shutdown = None
    try:
        record = await service.submit(
            "shutdown-creation-key-12345", "保存する作業環境", "container"
        )
        assert await asyncio.to_thread(entered.wait, 2)
        shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        await asyncio.wait_for(stopping.wait(), 2)
        for _ in range(2):
            shutdown.cancel()
            await asyncio.sleep(0)
        assert not shutdown.done()
        client.close.assert_not_called()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(shutdown), 2)
        client.close.assert_called_once()
        assert service.store.read(record.id).state == "succeeded"
        assert server._pool is None
    finally:
        release.set()
        if shutdown is None:
            await lifecycle.__aexit__(None, None, None)
        else:
            await asyncio.gather(shutdown, return_exceptions=True)
