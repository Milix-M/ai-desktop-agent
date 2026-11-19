import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock
from urllib.parse import urlsplit

import docker
import pytest
from docker.errors import APIError, NotFound
from httpx import ASGITransport
from requests.exceptions import ConnectionError as DockerConnectionError
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_control import (
    DockerUnavailableError,
    VmController,
    get_vm_controller,
)
from ai_desktop_agent.server.vm_pool import VmPool
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker


async def test_task_admission_without_any_environment_returns_conflict_without_saving_history(
    tmp_path, monkeypatch
):
    client = _FakeDocker()
    store = TaskStore(tmp_path / "data")
    with VmPool(client=client, state_file=tmp_path / "vms.json") as pool:
        monkeypatch.setattr(server, "_pool", pool)
        monkeypatch.setattr(server, "_store", store)
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            rejected = await api.post("/tasks", json={"instruction": "報告書を確認する"})
    assert rejected.status_code == 409 and rejected.json()["detail"] == "利用可能なVMがありません"
    assert store.list(limit=None) == [] and client.containers.runs == []
    assert server._sessions == {} and server._active_session is None


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.setenv("ALLOW_TCG_VM", "false")
    monkeypatch.delenv("VM_CONTAINER_NAME", raising=False)
    client = _FakeDocker()
    container = _FakeContainer(
        "mine-vm-1",
        labels={"com.docker.compose.project": "mine", "com.docker.compose.service": "vm"},
    )
    container.attrs["State"]["Running"] = True
    client.containers.items.append(container)
    controller = VmController(container_name=container.name, client=client)
    monkeypatch.setattr(controller, "_qmp_status", lambda: None)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    return client, container, controller, pool


@pytest.mark.parametrize(
    "error",
    [
        DockerConnectionError("daemon offline"),
        ReadTimeout("wait expired"),
        APIError("access denied"),
    ],
)
def test_a_lookup_failure_is_not_reported_as_a_missing_vm(managed, error):
    client, _, controller, _ = managed
    client.containers.get = Mock(side_effect=error)
    with pytest.raises(DockerUnavailableError):
        controller.status()


def test_an_actual_missing_named_container_is_reported_as_missing(managed):
    client, _, controller, _ = managed
    client.containers.get = Mock(side_effect=NotFound("gone"))
    assert controller.status().status == "not_found"


@pytest.mark.parametrize("reader", ["controller", "pool"])
def test_an_inspect_failure_does_not_return_cached_running_state(managed, reader):
    _, container, controller, pool = managed
    container.reload = Mock(side_effect=ReadTimeout("inspect response lost"))
    with pytest.raises(DockerUnavailableError):
        controller.status() if reader == "controller" else pool.list_vms()


@pytest.mark.parametrize("reader", ["controller", "pool"])
def test_a_container_removed_during_inspect_is_absent(managed, reader):
    _, container, controller, pool = managed
    container.reload = Mock(side_effect=NotFound("removed since listing"))
    if reader == "controller":
        assert controller.status().status == "not_found"
    else:
        assert pool.list_vms() == []


@pytest.mark.parametrize("operator", ["controller", "pool"])
def test_restart_response_loss_is_reported_without_repeating_the_command(managed, operator):
    _, container, controller, pool = managed
    container.restart = Mock(side_effect=ReadTimeout("restart may already have happened"))
    with pytest.raises(DockerUnavailableError, match="確認できません"):
        controller.restart() if operator == "controller" else pool.restart_vm("vm")
    container.restart.assert_called_once()


def test_docker_failure_is_not_misclassified_as_missing_kvm(managed, monkeypatch):
    client, _, controller, _ = managed
    monkeypatch.setenv("USE_KVM", "auto")
    monkeypatch.setattr("ai_desktop_agent.server.kvm.is_kvm_available", lambda _: False)
    client.containers.get = Mock(side_effect=ReadTimeout("daemon offline"))
    with pytest.raises(DockerUnavailableError):
        controller.restart()


@pytest.mark.parametrize("separator", ["-", "_"])
def test_status_and_controller_choose_the_actual_compose_vm(managed, separator):
    client, real, _, pool = managed
    real.name = f"mine{separator}vm{separator}1"
    shadow = _FakeContainer(
        "manual-shadow",
        labels={"com.docker.compose.project": "mine", "com.docker.compose.service": "vm"},
    )
    client.containers.items.insert(0, shadow)
    controller = VmController(client=client)
    assert pool.get_vm("vm").vnc_host == real.name
    assert controller._find_container().name == real.name


@pytest.mark.parametrize("phase", ["lookup", "inspect", "restart"])
async def test_management_endpoints_return_503_for_docker_failures(managed, monkeypatch, phase):
    client, container, controller, _ = managed
    monkeypatch.setattr(server, "_get_vm_controller", lambda: controller)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    if phase == "lookup":
        client.containers.get = Mock(side_effect=ReadTimeout("lookup timed out"))
    elif phase == "inspect":
        container.reload = Mock(side_effect=ReadTimeout("inspect timed out"))
    else:
        container.restart = Mock(side_effect=ReadTimeout("restart timed out"))
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await (api.post("/vm/restart") if phase == "restart" else api.get("/vm/status"))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]


async def test_vm_list_does_not_return_cached_state_after_an_inspect_failure(managed, monkeypatch):
    _, container, _, pool = managed
    pool.isolate_environments = Mock()
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    container.reload = Mock(side_effect=ReadTimeout("inspect timed out"))
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await api.get("/vms")
    assert response.status_code == 503, response.text


@pytest.fixture
def engine_endpoint(monkeypatch):
    """Exercise the real SDK over HTTP against a controlled, disposable upstream."""
    state = {"phase": "healthy", "restart_calls": 0}
    container_id = "f" * 64
    inspect = {
        "Id": container_id,
        "Name": "/fixture-vm",
        "Config": {"Labels": {}},
        "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}},
    }

    class EngineHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _respond(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _lose_response(self):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/version":
                self._respond(200, {"ApiVersion": "1.48", "MinAPIVersion": "1.24"})
                return
            if path == "/v1.48/_ping":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")
                return
            if path == "/v1.48/containers/fixture-vm/json":
                if state["phase"] == "lookup_close":
                    self._lose_response()
                    return
                if state["phase"] == "missing":
                    self._respond(404, {"message": "No such container"})
                    return
            elif path == f"/v1.48/containers/{container_id}/json":
                if state["phase"] == "inspect_500":
                    self._respond(500, {"message": "inspect unavailable"})
                    return
            else:
                self._respond(404, {"message": "unexpected endpoint"})
                return
            self._respond(200, inspect)

        def do_POST(self):
            if urlsplit(self.path).path != f"/v1.48/containers/{container_id}/restart":
                self._respond(404, {"message": "unexpected endpoint"})
                return
            state["restart_calls"] += 1
            if state["phase"] == "restart_close":
                self._lose_response()
                return
            self._respond(204, {})

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), EngineHandler)
    # Poll shutdown promptly; socket and client timeouts remain unchanged.
    worker = threading.Thread(
        target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    client = docker.DockerClient(
        base_url=f"http://127.0.0.1:{upstream.server_port}", version="1.48", timeout=0.5
    )
    state["base_url"] = f"http://127.0.0.1:{upstream.server_port}"
    monkeypatch.setattr(VmController, "_qmp_status", lambda _: None)
    monkeypatch.setattr(
        server,
        "_get_vm_controller",
        lambda: VmController(container_name="fixture-vm", client=client),
    )
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    try:
        yield state
    finally:
        client.close()
        upstream.shutdown()
        upstream.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("phase", ["lookup_close", "inspect_500", "restart_close", "missing"])
async def test_management_preserves_sdk_errors_over_http_and_recovers(engine_endpoint, phase):
    engine_endpoint["phase"] = phase
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await (
            api.post("/vm/restart") if phase == "restart_close" else api.get("/vm/status")
        )
        if phase == "missing":
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "not_found"
        else:
            assert response.status_code == 503, response.text
            assert response.json()["detail"]
        if phase == "restart_close":
            assert engine_endpoint["restart_calls"] == 1
        engine_endpoint["phase"] = "healthy"
        recovered = await api.get("/vm/status")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["running"] is True
    assert recovered.json()["health"] == "healthy"


@pytest.mark.parametrize("unavailable", ["missing-vm", "disabled-kvm"])
async def test_default_controller_rejects_unavailable_restart_before_a_docker_mutation(
    engine_endpoint, monkeypatch, unavailable
):
    monkeypatch.setenv("DOCKER_HOST", engine_endpoint["base_url"])
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
    monkeypatch.setenv("VM_CONTAINER_NAME", "fixture-vm")
    monkeypatch.setattr(server, "_get_vm_controller", get_vm_controller)
    if unavailable == "missing-vm":
        engine_endpoint["phase"] = "missing"
    else:
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.setenv("ALLOW_TCG_VM", "false")
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        rejected = await api.post("/vm/restart")
        assert rejected.status_code == (503 if unavailable == "missing-vm" else 409)
        assert rejected.json()["detail"]
        assert engine_endpoint["restart_calls"] == 0
        engine_endpoint["phase"] = "healthy"
        monkeypatch.setenv("ALLOW_TCG_VM", "true")
        accepted = await api.post("/vm/restart")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["running"] is True and accepted.json()["health"] == "healthy"
    assert engine_endpoint["restart_calls"] == 1
