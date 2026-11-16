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
