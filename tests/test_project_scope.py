import pytest

from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.mark.parametrize("operation", ["stop_vm", "start_vm", "restart_vm", "remove_vm"])
@pytest.mark.parametrize("source", ["environment", "backend"])
def test_fixed_operations_leave_other_projects_untouched(tmp_path, monkeypatch, operation, source):
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    docker = _FakeDocker()
    if source == "environment":
        monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    else:
        import socket

        docker.containers.items.append(
            _FakeContainer(socket.gethostname(), labels={"com.docker.compose.project": "mine"})
        )
    targets = []
    outsiders = []
    for service in ("desktop", "websockify-desktop"):
        for project, group in (("mine", targets), ("other", outsiders)):
            container = _FakeContainer(
                f"{project}-{service}-1",
                labels={
                    "com.docker.compose.service": service,
                    "com.docker.compose.project": project,
                },
            )
            group.append(container)
            docker.containers.items.append(container)
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    assert pool.get_vm("desktop").vnc_host == "mine-desktop-1"
    getattr(pool, operation)("desktop")
    for c in outsiders:
        assert not any((c.stopped, c.started, c.restarted, c.removed))
    attr = {
        "stop_vm": "stopped",
        "start_vm": "started",
        "restart_vm": "restarted",
        "remove_vm": "removed",
    }[operation]
    assert getattr(targets[0], attr)


def test_legacy_controller_ignores_other_projects(monkeypatch):
    from ai_desktop_agent.server.vm_control import VmController

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    docker = _FakeDocker()
    for project in ("other", "mine"):
        docker.containers.items.append(
            _FakeContainer(
                f"{project}-vm-1",
                labels={
                    "com.docker.compose.service": "vm",
                    "com.docker.compose.project": project,
                },
            )
        )
    assert VmController(client=docker)._find_container().name == "mine-vm-1"


@pytest.mark.parametrize("operation", ["stop_vm", "start_vm", "restart_vm", "remove_vm"])
async def test_dynamic_operations_only_target_the_owning_project(tmp_path, monkeypatch, operation):
    from ai_desktop_agent.server.vm_pool import LABEL_KIND, LABEL_PROJECT, LABEL_VM_ID

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    docker = _FakeDocker()
    mine = _FakeContainer(
        "mine-desktop",
        labels={LABEL_VM_ID: "desk-shared", LABEL_KIND: "container", LABEL_PROJECT: "mine"},
    )
    other = _FakeContainer(
        "other-desktop",
        labels={LABEL_VM_ID: "desk-shared", LABEL_KIND: "container", LABEL_PROJECT: "other"},
    )
    docker.containers.items.extend([mine, other])
    pool = VmPool(
        client=docker, state_file=tmp_path / "state.json", overlays_dir=str(tmp_path / "disks")
    )
    assert pool.get_vm("desk-shared").vnc_host == mine.name
    getattr(pool, operation)("desk-shared")
    assert not any((other.stopped, other.started, other.restarted, other.removed))


def test_legacy_vm_requires_saved_allocation_and_backend_network(tmp_path, monkeypatch):
    from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, LABEL_VM_ID

    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    monkeypatch.setattr(pool, "_backend_network", lambda docker: "mine_default")
    legacy = _FakeContainer("old-desktop", labels={LABEL_VM_ID: "desk-legacy"})
    legacy.attrs["Config"]["Labels"].pop(LABEL_PROJECT)
    legacy.attrs["NetworkSettings"] = {"Networks": {"mine_default": {}}}
    docker.containers.items.append(legacy)
    assert pool.get_vm("desk-legacy") is None
    pool._save_state("desk-legacy", {"name": "旧環境"})
    assert pool.get_vm("desk-legacy").name == "旧環境"
    legacy.attrs["NetworkSettings"]["Networks"] = {"other_default": {}}
    assert pool.get_vm("desk-legacy") is None
    assert pool.remove_vm("desk-legacy") is False
    assert not legacy.removed


def test_dynamic_creation_labels_both_containers_and_ignores_relays_in_listing(
    tmp_path, monkeypatch
):
    from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, LABEL_WS_FOR

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    monkeypatch.setenv("DESKTOP_IMAGE_REF", "desktop:test")
    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    created = pool.create_vm(kind="container")
    assert all(run["labels"][LABEL_PROJECT] == "mine" for run in docker.containers.runs)
    main = next(c for c in docker.containers.items if LABEL_WS_FOR not in pool._labels(c))
    # Docker listing order must not determine whether the relay replaces the desktop.
    assert pool.get_vm(created.id).vnc_host == main.name
    docker.containers.items.reverse()
    assert pool.get_vm(created.id).vnc_host == main.name


def test_disk_cleanup_uses_backend_mount_and_keeps_disk_if_container_removal_fails(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from ai_desktop_agent.server.vm_control import DockerUnavailableError
    from ai_desktop_agent.server.vm_pool import LABEL_VM_ID

    host = tmp_path / "host-repo"
    backend = tmp_path / "backend-mount"
    backend.mkdir()
    monkeypatch.setenv("VM_HOST_DIR", str(host))
    docker = _FakeDocker()
    container = _FakeContainer("desktop", labels={LABEL_VM_ID: "vm-owned"})
    docker.containers.items.append(container)
    pool = VmPool(client=docker, state_file=tmp_path / "state.json", overlays_dir=str(backend))
    disk = backend / "vm-owned.qcow2"
    disk.write_bytes(b"disk")
    container.remove = Mock(side_effect=RuntimeError("Docker unavailable"))
    with pytest.raises(DockerUnavailableError):
        pool.remove_vm("vm-owned")
    assert disk.exists()
    container.remove = Mock()
    assert pool.remove_vm("vm-owned") is True
    assert not disk.exists()


def test_new_desktops_need_no_host_ports_even_when_other_projects_publish_them(tmp_path):
    docker = _FakeDocker()
    other = _FakeContainer("other-service")
    other.attrs["HostConfig"] = {"PortBindings": {"5900/tcp": [{"HostPort": "5910"}]}}
    docker.containers.items.append(other)
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    created = pool.create_vm(kind="container")
    assert created.vnc_port == created.ws_port == 0
    assert not docker.containers.runs[0].get("ports")
    assert not other.removed
