from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


def environment(client, name, health, fixed=False):
    labels = (
        {"com.docker.compose.service": "desktop"}
        if fixed
        else {"ai-desktop-agent.vm-id": name, "ai-desktop-agent.kind": "container"}
    )
    container = _FakeContainer(name, labels=labels, health=health)
    client.containers.items.append(container)
    return container


@pytest.mark.parametrize("health", ["starting", "unhealthy", "unknown"])
def test_tasks_reject_a_running_container_that_is_not_ready(tmp_path, monkeypatch, health):
    client = _FakeDocker()
    environment(client, "desk-waiting", health)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    monkeypatch.setattr(server, "_pool", pool)
    pool.prepare_vm = Mock()
    with pytest.raises(HTTPException) as error:
        server._resolve_vm("desk-waiting")
    assert error.value.status_code == 409
    pool.prepare_vm.assert_not_called()


@pytest.mark.parametrize("health", ["starting", "unhealthy"])
def test_the_default_target_skips_an_unready_fixed_desktop(tmp_path, health):
    client = _FakeDocker()
    environment(client, "desktop", health, fixed=True)
    environment(client, "desk-ready", "healthy")
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    assert pool.default_vm().id == "desk-ready"


def test_no_ready_environment_has_no_default_target(tmp_path):
    client = _FakeDocker()
    environment(client, "desktop", "unhealthy", fixed=True)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    assert pool.default_vm() is None


@pytest.mark.parametrize("health", [None, "healthy", "none"])
def test_ready_and_legacy_environments_remain_available(tmp_path, monkeypatch, health):
    client = _FakeDocker()
    environment(client, "desk-ready", health)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    monkeypatch.setattr(server, "_pool", pool)
    pool.prepare_vm = Mock()
    assert server._resolve_vm("desk-ready").id == "desk-ready"


@pytest.mark.parametrize("health", ["starting", "unhealthy"])
def test_the_viewer_can_still_inspect_a_running_guest_before_it_is_ready(
    tmp_path, monkeypatch, health
):
    client = _FakeDocker()
    environment(client, "desk-inspect", health)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    monkeypatch.setattr(server, "_pool", pool)
    pool.prepare_vm = Mock()
    assert server._resolve_vm("desk-inspect", require_ready=False).id == "desk-inspect"


@pytest.mark.parametrize(
    "exit_status,message", [(78, "VM実行イメージの更新"), (1, "VMディスクを作成できません")]
)
def test_copy_failure_is_reported_and_cleans_up_the_allocation(
    tmp_path, monkeypatch, exit_status, message
):
    from docker.errors import ContainerError

    from ai_desktop_agent.server.vm_control import DockerUnavailableError

    client = _FakeDocker()
    original = client.containers.run
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_TCG_VM", "true")

    def fail_copy(image, **kwargs):
        if kwargs.get("remove"):
            raise ContainerError(None, exit_status, kwargs.get("command"), image, b"failed")
        return original(image, **kwargs)

    client.containers.run = fail_copy
    pool = VmPool(
        client=client, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path / "disks")
    )
    with pytest.raises(DockerUnavailableError, match=message):
        pool.create_vm()
    assert not list((tmp_path / "disks").iterdir())
    assert pool._load_all_states() == {}


def test_a_timed_out_copy_helper_is_removed_before_its_disk_directory(tmp_path, monkeypatch):
    from ai_desktop_agent.server.vm_control import DockerUnavailableError

    client = _FakeDocker()
    helpers = []
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_TCG_VM", "true")

    def lose_wait_response(image, **kwargs):
        helper = _FakeContainer("owned-copy", labels=kwargs.get("labels", {}))
        client.containers.items.append(helper)
        helpers.append(helper)
        raise RuntimeError("lost Docker wait response while helper is still running")

    client.containers.run = lose_wait_response
    pool = VmPool(
        client=client, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path / "disks")
    )
    with pytest.raises(DockerUnavailableError):
        pool.create_vm()
    assert helpers[0].removed
    assert not list((tmp_path / "disks").iterdir())


def test_a_copy_helper_is_not_exposed_as_an_environment_or_a_task_target(tmp_path):
    from ai_desktop_agent.server.vm_pool import LABEL_DISK_COPY, LABEL_VM_ID

    client = _FakeDocker()
    helper = _FakeContainer(
        "copy", labels={LABEL_VM_ID: "vm-creating", LABEL_DISK_COPY: "vm-creating"}
    )
    client.containers.items.append(helper)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    assert pool.list_vms() == []
    assert pool.instance_id("vm-creating") is None
