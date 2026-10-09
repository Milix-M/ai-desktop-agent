"""動的VMプールのテスト（Dockerはフェイク）。"""

import pytest

from ai_desktop_agent.server.vm_pool import VmPool


class _FakeContainer:
    def __init__(self, name, status="running", labels=None, health=None):
        self.name = name
        self.status = status
        self.attrs = {
            "Config": {"Labels": labels or {}},
            "Image": "test-vm-image:latest",
            "State": {"Health": {"Status": health}} if health else {},
        }
        self.stopped = False
        self.removed = False

    def reload(self):
        pass

    def stop(self, timeout=10):
        self.stopped = True
        self.status = "exited"

    def restart(self, timeout=30):
        self.restarted = True
        self.status = "running"

    def remove(self, force=False):
        self.removed = True


class _FakeContainers:
    def __init__(self):
        self.items: list[_FakeContainer] = []
        self.runs: list[dict] = []

    def list(self, all=True, filters=None):
        items = [c for c in self.items if not c.removed]
        label = (filters or {}).get("label", "")
        if label and "=" in label:
            k, v = label.split("=", 1)
            return [
                c for c in items if ((c.attrs.get("Config") or {}).get("Labels") or {}).get(k) == v
            ]
        return list(items)

    def get(self, name):
        for c in self.items:
            if c.name == name:
                return c
        raise ValueError(f"no such container: {name}")

    def run(self, image, **kwargs):
        self.runs.append({"image": image, **kwargs})
        if kwargs.get("remove"):
            return None
        c = _FakeContainer(
            name=kwargs.get("name", "new"),
            status="running",
            labels=kwargs.get("labels", {}),
        )
        self.items.append(c)
        return c


class _FakeDocker:
    def __init__(self):
        self.containers = _FakeContainers()

    def ping(self):
        return True


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("VM_IMAGE_REF", "test-vm-image:latest")
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.delenv("BACKEND_NETWORK", raising=False)
    client = _FakeDocker()
    return VmPool(
        client=client,
        state_file=tmp_path / "vms.json",
        overlays_dir=str(tmp_path / "vm" / "overlays"),
    ), client


class TestListVms:
    def test_discovers_legacy_vm(self, pool):
        pool, client = pool
        client.containers.items.append(
            _FakeContainer(
                name="ai-desktop-agent-vm-1",
                labels={"com.docker.compose.service": "vm"},
                health="healthy",
            )
        )
        vms = pool.list_vms()
        assert len(vms) == 1
        assert vms[0].id == "vm"
        assert vms[0].managed is False
        assert vms[0].health == "healthy"

    def test_empty(self, pool):
        pool, _ = pool
        assert pool.list_vms() == []
        assert pool.default_vm() is None
        assert pool.get_vm("vm") is None

    def test_default_prefers_running(self, pool):
        pool, client = pool
        client.containers.items.append(
            _FakeContainer(name="a", status="exited", labels={"ai-desktop-agent.vm-id": "vm-a"})
        )
        assert pool.default_vm() is None  # 停止中のみ
        client.containers.items.append(
            _FakeContainer(name="b", status="running", labels={"ai-desktop-agent.vm-id": "vm-b"})
        )
        assert pool.default_vm().id == "vm-b"


class TestCreateRemove:
    def test_create_vm(self, pool):
        pool, client = pool
        info = pool.create_vm(name="test2")
        assert info.id.startswith("vm-")
        assert info.vnc_port == 5910
        assert info.ws_port == 6090
        # overlay作成 + vm + websockify の3 run
        assert len(client.containers.runs) == 3
        overlay_run = client.containers.runs[0]
        assert overlay_run.get("entrypoint") == ["cp"]
        vm_run = client.containers.runs[1]
        assert vm_run["ports"] == {"5900/tcp": 5910}
        assert vm_run["labels"]["ai-desktop-agent.vm-id"].startswith("vm-")
        ws_run = client.containers.runs[2]
        assert ws_run["ports"] == {"6080/tcp": 6090}
        # 一覧に出る
        assert pool.get_vm(info.id).id == info.id

    def test_port_allocation_skips_used(self, pool):
        pool, client = pool
        a = pool.create_vm()
        b = pool.create_vm()
        assert (a.vnc_port, a.ws_port) == (5910, 6090)
        assert (b.vnc_port, b.ws_port) == (5911, 6091)

    def test_remove_vm(self, pool, tmp_path):
        pool, client = pool
        info = pool.create_vm()
        overlay = tmp_path / "vm" / "overlays" / f"{info.id}.qcow2"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("fake")
        assert pool.remove_vm(info.id) is True
        assert pool.get_vm(info.id) is None
        assert not overlay.exists()

    def test_remove_missing_returns_false(self, pool):
        pool, _ = pool
        assert pool.remove_vm("vm-nope") is False

    def test_remove_default_forbidden(self, pool):
        pool, _ = pool
        with pytest.raises(ValueError, match="既定VM"):
            pool.remove_vm("vm")

    def test_max_vms(self, pool, monkeypatch):  # noqa: ARG002
        import ai_desktop_agent.server.vm_pool as mod

        pool, _ = pool
        monkeypatch.setattr(mod, "MAX_VMS", 1)
        pool.create_vm()
        with pytest.raises(ValueError, match="上限"):
            pool.create_vm()


class TestVmEndpoints:
    @pytest.mark.asyncio
    async def test_vms_crud(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from ai_desktop_agent.server import app as server_app
        from ai_desktop_agent.server.vm_pool import VmPool as Pool

        monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
        monkeypatch.setenv("VM_IMAGE_REF", "test-vm-image:latest")
        monkeypatch.setenv("USE_KVM", "true")
        monkeypatch.delenv("BACKEND_NETWORK", raising=False)
        client = _FakeDocker()
        server_app._pool = Pool(
            client=client,
            state_file=tmp_path / "vms.json",
            overlays_dir=str(tmp_path / "vm" / "overlays"),
        )
        try:
            transport = ASGITransport(app=server_app.app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                assert (await c.get("/vms")).json() == []
                created = (await c.post("/vms", json={"name": "e2e"})).json()
                assert created["id"].startswith("vm-")
                assert created["ws_port"] == 6090
                listed = (await c.get("/vms")).json()
                assert len(listed) == 1
                assert (await c.delete(f"/vms/{created['id']}")).json() == {"status": "deleted"}
                assert (await c.delete("/vms/vm-nope")).status_code == 404
                assert (await c.delete("/vms/vm")).status_code == 400
        finally:
            server_app._pool = None

    @pytest.mark.asyncio
    async def test_vms_503_without_docker(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from ai_desktop_agent.server import app as server_app

        class _Dead:
            def list_vms(self):
                from ai_desktop_agent.server.vm_pool import DockerUnavailableError

                raise DockerUnavailableError("no socket")

        server_app._pool = _Dead()
        try:
            transport = ASGITransport(app=server_app.app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                assert (await c.get("/vms")).status_code == 503
        finally:
            server_app._pool = None


class TestRestartVm:
    def test_restart_legacy(self, pool):
        pool, client = pool
        client.containers.items.append(
            _FakeContainer(
                name="ai-desktop-agent-vm-1",
                labels={"com.docker.compose.service": "vm"},
            )
        )
        info = pool.restart_vm("vm")
        assert info.id == "vm"
        assert client.containers.items[0].restarted is True

    def test_restart_dynamic_skips_relay(self, pool):
        pool, client = pool
        vm_c = _FakeContainer(
            name="ai-desktop-agent-vm-x1",
            labels={"ai-desktop-agent.vm-id": "vm-x1"},
        )
        ws_c = _FakeContainer(
            name="ai-desktop-agent-ws-vm-x1",
            labels={
                "ai-desktop-agent.vm-id": "vm-x1",
                "ai-desktop-agent.ws-for": "vm-x1",
            },
        )
        client.containers.items.extend([vm_c, ws_c])
        pool.restart_vm("vm-x1")
        # 中継は再起動しない
        assert ws_c.status == "running"

    def test_restart_missing_raises(self, pool):
        pool, _ = pool
        with __import__("pytest").raises(ValueError, match="見つかりません"):
            pool.restart_vm("vm-nope")


class TestDesktopDiscovery:
    def test_discovers_desktop_with_default_ports(self, pool):
        from ai_desktop_agent.server.vm_pool import DESKTOP_SERVICE

        pool, client = pool
        client.containers.items.append(
            _FakeContainer(
                name="ai-desktop-agent-desktop-1",
                labels={"com.docker.compose.service": "desktop"},
                health="healthy",
            )
        )
        vms = pool.list_vms()
        assert len(vms) == 1
        assert vms[0].id == DESKTOP_SERVICE
        assert vms[0].managed is False
        assert vms[0].vnc_port == 5901
        assert vms[0].ws_port == 6081

    def test_default_prefers_desktop(self, pool):
        pool, client = pool
        client.containers.items.append(
            _FakeContainer(
                name="ai-desktop-agent-vm-1",
                labels={"com.docker.compose.service": "vm"},
            )
        )
        client.containers.items.append(
            _FakeContainer(
                name="ai-desktop-agent-desktop-1",
                labels={"com.docker.compose.service": "desktop"},
            )
        )
        assert pool.default_vm().id == "desktop"

    def test_remove_desktop_forbidden(self, pool):
        pool, _ = pool
        with pytest.raises(ValueError, match="既定VM"):
            pool.remove_vm("desktop")


class TestKvmGate:
    def test_create_blocked_without_kvm(self, pool, monkeypatch):
        from ai_desktop_agent.server.kvm import KvmUnavailableError

        pool, _ = pool
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        with pytest.raises(KvmUnavailableError):
            pool.create_vm()

    def test_create_allowed_with_tcg_override(self, pool, monkeypatch):
        pool, client = pool
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.setenv("ALLOW_TCG_VM", "true")
        info = pool.create_vm()
        assert info.id.startswith("vm-")

    def test_restart_blocked_without_kvm(self, pool, monkeypatch):
        from ai_desktop_agent.server.kvm import KvmUnavailableError

        pool, client = pool
        client.containers.items.append(
            _FakeContainer(
                name="ai-desktop-agent-vm-1",
                labels={"com.docker.compose.service": "vm"},
            )
        )
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        with pytest.raises(KvmUnavailableError):
            pool.restart_vm("vm")

    @pytest.mark.asyncio
    async def test_create_vm_409_without_kvm(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from ai_desktop_agent.server import app as server_app
        from ai_desktop_agent.server.vm_pool import VmPool as Pool

        monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
        monkeypatch.setenv("VM_IMAGE_REF", "test-vm-image:latest")
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        monkeypatch.delenv("BACKEND_NETWORK", raising=False)
        client = _FakeDocker()
        server_app._pool = Pool(
            client=client,
            state_file=tmp_path / "vms.json",
            overlays_dir=str(tmp_path / "vm" / "overlays"),
        )
        try:
            transport = ASGITransport(app=server_app.app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post("/vms", json={"name": "e2e"})
                assert resp.status_code == 409
        finally:
            server_app._pool = None


class TestContainerProvisioning:
    def test_create_container_without_kvm(self, pool, monkeypatch):
        pool, client = pool
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        info = pool.create_vm(name="c1", kind="container")
        assert info.id.startswith("desk-")
        assert info.kind == "container"
        assert info.vnc_port == 5910
        assert info.ws_port == 6090
        # overlay複製なしのため本体 + 中継の2 run のみ
        assert len(client.containers.runs) == 2
        desk_run = client.containers.runs[0]
        assert "devices" not in desk_run, "コンテナ作成で /dev/kvm を要求してはいけない"
        assert desk_run["labels"]["ai-desktop-agent.kind"] == "container"
        assert desk_run["environment"]["VNC_GEOMETRY"] == "1280x800"
        ws_run = client.containers.runs[1]
        assert ws_run["ports"] == {"6080/tcp": 6090}
        assert pool.get_vm(info.id).kind == "container"

    def test_create_invalid_kind(self, pool):
        pool, _ = pool
        with pytest.raises(ValueError, match="kind"):
            pool.create_vm(kind="lxc")

    def test_qemu_still_blocked_without_kvm(self, pool, monkeypatch):
        from ai_desktop_agent.server.kvm import KvmUnavailableError

        pool, _ = pool
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        with pytest.raises(KvmUnavailableError):
            pool.create_vm(kind="qemu")

    def test_restart_container_without_kvm(self, pool, monkeypatch):
        pool, client = pool
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        info = pool.create_vm(kind="container")
        main = next(c for c in client.containers.items if c.name == f"ai-desktop-agent-{info.id}")
        out = pool.restart_vm(info.id)
        assert out.id == info.id
        assert main.restarted is True

    def test_remove_container(self, pool, monkeypatch):
        pool, _ = pool
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        info = pool.create_vm(kind="container")
        assert pool.remove_vm(info.id) is True
        assert pool.get_vm(info.id) is None

    def test_kind_label_fallback(self, pool):
        pool, client = pool
        # kindラベルなしの旧コンテナは qemu 扱い（後方互換）
        client.containers.items.append(
            _FakeContainer(name="x", labels={"ai-desktop-agent.vm-id": "vm-old"})
        )
        assert pool.get_vm("vm-old").kind == "qemu"

    @pytest.mark.asyncio
    async def test_create_container_endpoint(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from ai_desktop_agent.server import app as server_app
        from ai_desktop_agent.server.vm_pool import VmPool as Pool

        monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
        monkeypatch.setenv("DESKTOP_IMAGE_REF", "test-desktop-image:latest")
        monkeypatch.setenv("USE_KVM", "false")
        monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
        monkeypatch.delenv("BACKEND_NETWORK", raising=False)
        client = _FakeDocker()
        server_app._pool = Pool(
            client=client,
            state_file=tmp_path / "vms.json",
            overlays_dir=str(tmp_path / "vm" / "overlays"),
        )
        try:
            transport = ASGITransport(app=server_app.app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                created = (await c.post("/vms", json={"kind": "container"})).json()
                assert created["id"].startswith("desk-")
                assert created["kind"] == "container"
                assert (await c.post("/vms", json={"kind": "lxc"})).status_code == 400
                # qemu は KVMなしで409
                assert (await c.post("/vms", json={"kind": "qemu"})).status_code == 409
        finally:
            server_app._pool = None
