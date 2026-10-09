"""VM管理（作り直し機能）のテスト。"""

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.server import app as server_app
from ai_desktop_agent.server.vm_control import (
    DockerUnavailableError,
    VmController,
    VmStatusInfo,
)


class _FakeContainer:
    def __init__(self, name="vm-1", status="running", running=True, health="healthy"):
        self.name = name
        self.status = status
        self.attrs = {"State": {"Running": running, "Health": {"Status": health}}}
        self.restarted = False

    def reload(self):
        pass

    def restart(self, timeout=30):
        self.restarted = True
        self.status = "restarting"


class _FakeContainers:
    def __init__(self, containers):
        self._containers = {c.name: c for c in containers}

    def get(self, name):
        return self._containers[name]

    def list(self, all=True, filters=None):
        return list(self._containers.values())


class _FakeDockerClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)

    def ping(self):
        return True


class TestVmControllerStatus:
    def test_running(self):
        ctrl = VmController(client=_FakeDockerClient([_FakeContainer()]))
        info = ctrl.status()
        assert info.running is True
        assert info.status == "running"
        assert info.health == "healthy"

    def test_not_found(self):
        ctrl = VmController(client=_FakeDockerClient([]))
        info = ctrl.status()
        assert info.running is False
        assert info.status == "not_found"

    def test_explicit_name_missing(self):
        ctrl = VmController(container_name="nope", client=_FakeDockerClient([]))
        assert ctrl._find_container() is None

    def test_no_docker_daemon(self, monkeypatch):
        import docker

        def _boom():
            raise ConnectionError("no socket")

        monkeypatch.setattr(docker, "from_env", _boom)
        with pytest.raises(DockerUnavailableError):
            VmController(client=None)._find_container()


class TestVmControllerRestart:
    def test_restart_calls_docker(self, monkeypatch):
        monkeypatch.setenv("USE_KVM", "true")
        c = _FakeContainer()
        ctrl = VmController(client=_FakeDockerClient([c]))
        info = ctrl.restart()
        assert c.restarted is True
        assert info.status == "restarting"

    def test_restart_not_found_raises(self, monkeypatch):
        monkeypatch.setenv("USE_KVM", "true")
        ctrl = VmController(client=_FakeDockerClient([]))
        with pytest.raises(DockerUnavailableError):
            ctrl.restart()


@pytest.fixture
def _vm_app(monkeypatch):
    """_get_vm_controller をフェイクに差し替える。"""
    monkeypatch.setenv("USE_KVM", "true")
    server_app._active_session = None
    fake = VmController(client=_FakeDockerClient([_FakeContainer()]))
    orig = server_app._get_vm_controller
    server_app._get_vm_controller = lambda: fake
    yield fake
    server_app._get_vm_controller = orig
    server_app._active_session = None


@pytest.mark.asyncio
class TestVmEndpoints:
    async def test_status(self, _vm_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/vm/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["running"] is True
            assert data["health"] == "healthy"

    async def test_restart(self, _vm_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/vm/restart")
            assert resp.status_code == 200
            assert resp.json()["status"] == "restarting"

    async def test_status_503_without_docker(self):
        orig = server_app._get_vm_controller

        class _Dead:
            def status(self):
                raise DockerUnavailableError("no socket")

            def restart(self):
                raise DockerUnavailableError("no socket")

        server_app._get_vm_controller = lambda: _Dead()
        try:
            transport = ASGITransport(app=server_app.app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/vm/status")
                assert resp.status_code == 503
                resp2 = await client.post("/vm/restart")
                assert resp2.status_code == 503
        finally:
            server_app._get_vm_controller = orig


def test_status_info_defaults():
    info = VmStatusInfo(running=False, status="exited")
    assert info.health is None
    assert info.name is None
