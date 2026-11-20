"""Docker clients belong to one service or operation, including failed initialization."""

import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import docker
import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.vm_control import DockerUnavailableError, VmController
from ai_desktop_agent.server.vm_pool import VmPool
from tests.http import AuthenticatedClient
from tests.test_vm_control import _FakeContainer


@pytest.mark.parametrize("owner_type", [VmController, VmPool])
@pytest.mark.parametrize("close_fails", [False, True])
def test_failed_ping_closes_the_client_and_allows_a_fresh_retry(
    monkeypatch, owner_type, close_fails
):
    failed = Mock()
    failed.ping.side_effect = ConnectionError("ping failed")
    if close_fails:
        failed.close.side_effect = RuntimeError("close failed")
    healthy = Mock()
    factory = Mock(side_effect=[failed, healthy])
    monkeypatch.setattr(docker, "from_env", factory)
    owner = owner_type()
    with pytest.raises(DockerUnavailableError) as error:
        owner._docker()
    assert isinstance(error.value.__cause__, ConnectionError)
    failed.close.assert_called_once()
    assert owner._docker() is healthy
    assert owner._docker() is healthy
    assert factory.call_count == 2
    healthy.ping.assert_called_once()


@pytest.mark.parametrize("owner_type", [VmController, VmPool])
def test_concurrent_first_use_allocates_one_shared_client(monkeypatch, owner_type):
    ready = threading.Barrier(4)
    allocated = []

    def connect():
        client = Mock()
        allocated.append(client)
        # Keep initialization in flight while the other callers enter the service.
        time.sleep(0.03)
        return client

    monkeypatch.setattr(docker, "from_env", connect)
    owner = owner_type()

    def acquire(_):
        ready.wait(timeout=2)
        return owner._docker()

    with ThreadPoolExecutor(max_workers=4) as workers:
        clients = list(workers.map(acquire, range(4)))
    assert len(allocated) == 1
    assert all(client is clients[0] for client in clients)
    clients[0].ping.assert_called_once()


@pytest.mark.parametrize("operation", ["status", "restart"])
@pytest.mark.parametrize("fails", [False, True])
async def test_each_vm_api_operation_closes_its_owned_docker_client(monkeypatch, operation, fails):
    container = _FakeContainer()
    if fails:
        container.reload = Mock(side_effect=ConnectionError("Docker disconnected"))
    client = Mock()
    client.containers.get.return_value = container
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.setattr(server, "_get_vm_controller", lambda: VmController(container_name="vm-1"))
    monkeypatch.setattr(VmController, "_qmp_status", lambda _: None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as browser:
        response = await (
            browser.get("/vm/status") if operation == "status" else browser.post("/vm/restart")
        )
    assert response.status_code == (503 if fails else 200)
    client.close.assert_called_once()


@pytest.mark.parametrize("owner_type", [VmController, VmPool])
def test_borrowed_clients_are_never_closed_by_the_service(owner_type):
    client = Mock()
    with owner_type(client=client) as owner:
        assert owner._docker() is client
    owner.close()
    with pytest.raises(DockerUnavailableError, match="終了"):
        owner._docker()
    client.close.assert_not_called()
    client.ping.assert_not_called()


@pytest.mark.parametrize("close_fails", [False, True])
def test_closed_services_cannot_silently_reopen_connections(monkeypatch, close_fails, caplog):
    first = Mock()
    if close_fails:
        first.close.side_effect = RuntimeError("close failed")
    factory = Mock(return_value=first)
    monkeypatch.setattr(docker, "from_env", factory)
    owner = VmPool()
    with owner:
        assert owner._docker() is first
    owner.close()
    first.close.assert_called_once()
    with pytest.raises(DockerUnavailableError, match="終了"):
        owner._docker()
    with pytest.raises(DockerUnavailableError, match="終了"), owner:
        pytest.fail("a closed service reopened")
    with pytest.raises(DockerUnavailableError, match="終了"):
        owner.list_vms()
    factory.assert_called_once()
    assert ("close failed" in caplog.text) is close_fails


@pytest.mark.parametrize("owner_type", [VmController, VmPool])
def test_an_interrupt_during_ping_closes_the_client_without_changing_the_interrupt(
    monkeypatch, owner_type
):
    client = Mock()
    client.ping.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    with pytest.raises(KeyboardInterrupt):
        owner_type()._docker()
    client.close.assert_called_once()


def test_missing_docker_package_produces_the_service_error_without_allocating(monkeypatch):
    monkeypatch.setitem(sys.modules, "docker", None)
    with pytest.raises(DockerUnavailableError, match="パッケージ"):
        VmPool()._docker()


@pytest.mark.parametrize("operation", ["status", "restart"])
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_disconnecting_a_vm_request_drains_the_worker_before_closing_its_client(
    monkeypatch, operation, cancellations
):
    entered, release = threading.Event(), threading.Event()
    client = Mock()
    client.containers.get.return_value = _FakeContainer()

    def ping():
        entered.set()
        assert release.wait(3)
        client.close.assert_not_called()

    client.ping.side_effect = ping
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.setattr(server, "_get_vm_controller", lambda: VmController(container_name="vm-1"))
    monkeypatch.setattr(VmController, "_qmp_status", lambda _: None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as browser:
        task = asyncio.create_task(
            browser.get("/vm/status") if operation == "status" else browser.post("/vm/restart")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
            client.close.assert_not_called()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 2)
            client.close.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


def test_shutdown_allows_an_inflight_operation_to_finish_nested_calls_but_rejects_new_work(
    monkeypatch,
):
    entered, release, closing = (threading.Event() for _ in range(3))
    client = Mock()
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    owner = VmPool()
    monkeypatch.setattr(owner, "_fixed_containers", lambda *args: [])
    monkeypatch.setattr(owner, "_dynamic_containers", lambda *args: [])
    list_vms = owner.list_vms

    def blocked_list():
        entered.set()
        assert release.wait(3)
        client.close.assert_not_called()
        return list_vms()

    monkeypatch.setattr(owner, "list_vms", blocked_list)

    def close():
        with owner._client_lock:
            closing.set()
            owner.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(owner.get_vm, "missing")
        cleanup = None
        try:
            assert entered.wait(2)
            cleanup = workers.submit(close)
            assert closing.wait(2)
            with owner._client_lock:
                assert owner._closing
            with pytest.raises(DockerUnavailableError, match="終了"):
                owner.get_instance("late-request")
            release.set()
            assert running.result(timeout=2) is None
            cleanup.result(timeout=2)
            client.close.assert_called_once()
        finally:
            release.set()


def test_closing_from_inside_an_operation_fails_instead_of_waiting_on_itself(monkeypatch):
    owner = VmController()
    monkeypatch.setattr(owner, "_find_container", owner.close)
    with pytest.raises(RuntimeError, match="実行中"):
        owner.status()
    owner.close()


@pytest.mark.parametrize("owner_type", [VmController, VmPool])
def test_close_waits_for_an_inflight_service_operation(monkeypatch, owner_type):
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    client = Mock()
    client.close.side_effect = closed.set
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    owner = owner_type()

    def lookup(*args):
        owner._docker()
        entered.set()
        assert release.wait(3)
        client.close.assert_not_called()
        return None

    if owner_type is VmPool:
        monkeypatch.setattr(owner, "_primary_container", lookup)
    else:
        monkeypatch.setattr(owner, "_find_container", lookup)

    def operation():
        return owner.get_instance("desk-running") if owner_type is VmPool else owner.status()

    def close():
        closing.set()
        owner.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(operation)
        cleanup = None
        try:
            assert entered.wait(2)
            cleanup = workers.submit(close)
            assert closing.wait(2)
            assert not closed.wait(0.05), "closed Docker while a service operation still used it"
            release.set()
            running.result(timeout=2)
            cleanup.result(timeout=2)
            client.close.assert_called_once()
        finally:
            release.set()
