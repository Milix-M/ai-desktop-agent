"""Deletion failures preserve ownership and allow safe, explicit cleanup retries."""

import json
from unittest.mock import Mock

import pytest
from docker.errors import APIError

from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.workspaces import WorkspaceVolumes
from tests.test_vm_image_resolution import environment as environment


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("failure", ["stop", "remove"])
def test_failed_shutdown_can_be_force_removed_but_failed_removal_preserves_resources(
    environment, tmp_path, managed, failure
):
    pool, client, primary = environment("container")
    if managed:
        identifier = pool.create_vm(kind="container").id
        target = client.containers.items[-1]
    else:
        identifier, target = "desktop", primary
        WorkspaceVolumes(client, "ai-desktop-agent").ensure(identifier)
    state_file = tmp_path / "vms.json"
    before = json.loads(state_file.read_text()) if state_file.exists() else {}
    volumes = set(client.volumes.items)
    original_remove = target.remove
    if failure == "stop":
        target.stop = Mock(side_effect=APIError("stop acknowledgement lost"))
        assert pool.remove_vm(identifier) is True
        assert target.removed
        assert set(client.volumes.items) == (set() if managed else volumes)
        assert json.loads(state_file.read_text()) == {}
    else:
        target.remove = Mock(side_effect=APIError("remove response unavailable"))
        with pytest.raises(DockerUnavailableError, match="削除"):
            pool.remove_vm(identifier)
        assert not target.removed and set(client.volumes.items) == volumes
        assert (json.loads(state_file.read_text()) if state_file.exists() else {}) == before
        target.remove = original_remove
        assert pool.remove_vm(identifier) is True
    if managed:
        assert not primary.removed and not primary.stopped


def test_disk_cleanup_failure_preserves_the_record_until_the_storage_is_repaired(
    environment, tmp_path
):
    pool, client, primary = environment("container")
    identifier = pool.create_vm(kind="container").id
    blocker = tmp_path / "overlays"
    blocker.write_text("pre-existing data")
    with pytest.raises(DockerUnavailableError, match="ディスク"):
        pool.remove_vm(identifier)
    assert blocker.read_text() == "pre-existing data"
    assert identifier in json.loads((tmp_path / "vms.json").read_text())
    assert client.volumes.items == {}
    blocker.unlink()
    blocker.mkdir()
    assert pool.remove_vm(identifier) is True
    assert json.loads((tmp_path / "vms.json").read_text()) == {}
    assert not primary.removed and not primary.stopped
