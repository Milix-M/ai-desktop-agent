"""Keep the filesystem failure and reconcile uncertain helper removal."""

from unittest.mock import Mock

import pytest
from docker.errors import APIError, NotFound
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.workspaces import HOME, LABEL_HELPER, LABEL_PROJECT, WorkspaceVolumes
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def workspace(monkeypatch):
    client = _FakeDocker()
    volumes = WorkspaceVolumes(client, "ai-desktop-agent", "backend-image")
    volume = volumes.ensure("desktop")
    get = client.containers.get

    def existing(identity):
        container = get(identity)
        if container.removed:
            raise NotFound("the helper has already been removed")
        return container

    monkeypatch.setattr(client.containers, "get", existing)
    return volumes, volume, client


@pytest.mark.parametrize("fails", [False, True])
def test_lost_removal_response_is_reconciled_without_masking_the_home_result(workspace, fails):
    volumes, volume, client = workspace
    fault = StorageError("home archive is damaged")

    def run():
        with volumes.helper(volume) as helper:
            remove = helper.remove

            def lost(**kwargs):
                remove(**kwargs)
                raise ReadTimeout("remove response lost")

            helper.remove = Mock(side_effect=lost)
            if fails:
                raise fault

    if fails:
        with pytest.raises(StorageError) as raised:
            run()
        assert raised.value is fault
    else:
        run()
    assert client.containers.items[-1].removed
    assert volume.name in client.volumes.items


@pytest.mark.parametrize("fault", [StorageError("archive failed"), KeyboardInterrupt()])
def test_failed_home_operation_and_failed_cleanup_both_retain_their_evidence(workspace, fault):
    volumes, volume, client = workspace
    cleanup_fault = APIError("cannot remove helper")
    with pytest.raises(BaseExceptionGroup) as raised, volumes.helper(volume) as helper:
        helper.remove = Mock(side_effect=cleanup_fault)
        raise fault
    assert raised.value.exceptions == (fault, cleanup_fault)
    assert any(helper.id in note and volume.name in note for note in cleanup_fault.__notes__)
    assert not helper.removed and volume.name in client.volumes.items


def test_unconfirmed_cleanup_after_a_successful_operation_reports_the_remaining_helper(workspace):
    volumes, volume, _ = workspace
    fault = ReadTimeout("remove timed out before acknowledgement")
    with pytest.raises(ReadTimeout) as raised, volumes.helper(volume) as helper:
        helper.remove = Mock(side_effect=fault)
    assert raised.value is fault
    assert any(helper.id in note for note in fault.__notes__)
    assert not helper.removed


def test_unknown_cleanup_state_preserves_the_original_error_and_delete_inspection_chain(
    workspace, monkeypatch
):
    volumes, volume, client = workspace
    original = StorageError("restore failed")
    removal, inspection = ReadTimeout("remove failed"), ReadTimeout("inspect failed")
    with pytest.raises(ExceptionGroup) as raised, volumes.helper(volume) as helper:
        helper.remove = Mock(side_effect=removal)
        monkeypatch.setattr(client.containers, "get", Mock(side_effect=inspection))
        raise original
    assert raised.value.exceptions == (original, inspection)
    assert inspection.__context__ is removal
    assert any(helper.id in note for note in inspection.__notes__)


def test_docker_transport_failure_does_not_create_a_volume_or_hide_its_cause(
    workspace, monkeypatch
):
    volumes, _, client = workspace
    fault = ReadTimeout("Docker unavailable")
    monkeypatch.setattr(client.volumes, "get", Mock(side_effect=fault))
    with pytest.raises(DockerUnavailableError) as raised:
        volumes.ensure("desk-reports")
    assert raised.value.__cause__ is fault
    assert len(client.volumes.items) == 1


@pytest.mark.parametrize("foreign", [False, True])
def test_mount_detection_skips_other_mounts_and_rejects_a_foreign_home(workspace, foreign):
    volumes, volume, _ = workspace
    source = _FakeContainer("desktop")
    source.attrs["Mounts"] = [
        {"Type": "bind", "Destination": "/other"},
        {"Type": "volume", "Name": volume.name, "Destination": HOME},
    ]
    if foreign:
        volume.attrs["Labels"][LABEL_PROJECT] = "other"
        with pytest.raises(DockerUnavailableError, match="所属"):
            volumes.mounted(source, "desktop")
    else:
        assert volumes.mounted(source, "desktop") is True


def test_an_attached_home_cannot_be_restored_and_remove_can_retry_an_absent_volume(workspace):
    volumes, volume, client = workspace
    source = _FakeContainer("stopped-desktop", status="exited")
    source.attrs["Mounts"] = [{"Type": "volume", "Name": volume.name, "Destination": HOME}]
    client.containers.items.append(source)
    with pytest.raises(DockerUnavailableError, match="使用中"):
        volumes.require_unused(volume)
    assert not source.removed
    source.remove()
    volumes.remove("desktop")
    volumes.remove("desktop")
    assert not client.volumes.items


@pytest.mark.parametrize("fault", ["foreign-volume", "wrong-volume-name", "no-owner", "relabel"])
def test_helper_reaping_leaves_ambiguous_or_changed_ownership_alone(workspace, fault):
    volumes, volume, client = workspace
    helper = _FakeContainer(
        "old-helper", labels={LABEL_HELPER: "true", "ai-desktop-agent.helper-owner": "gone-owner"}
    )
    helper.attrs.update(
        {
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True},
            "Mounts": [{"Type": "volume", "Name": volume.name, "Destination": HOME}],
        }
    )
    client.containers.items.append(helper)
    if fault == "foreign-volume":
        volume.attrs["Labels"][LABEL_PROJECT] = "other"
    elif fault == "wrong-volume-name":
        volume.name = "unexpected-home-volume"
    elif fault == "no-owner":
        del helper.attrs["Config"]["Labels"]["ai-desktop-agent.helper-owner"]
    else:

        def reload():
            helper.attrs["Config"]["Labels"][LABEL_PROJECT] = "other"

        helper.reload = reload
    volumes.reap_helpers()
    assert not helper.removed and len(client.volumes.items) == 1
