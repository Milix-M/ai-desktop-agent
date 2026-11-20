"""Preserve desktop execution state across backup and Docker response failures."""

from unittest.mock import Mock

import pytest
from docker.errors import APIError
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from tests.test_vm_pool import _FakeContainer, _FakeDocker
from tests.test_workspaces import archive_bytes


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    client = _FakeDocker()
    source = _FakeContainer(
        "ai-desktop-agent-desktop-1", labels={"com.docker.compose.service": "desktop"}
    )
    client.containers.items.append(source)

    def pause():
        source.status = "paused"

    def unpause():
        if source.status != "paused":
            raise APIError("container is not paused")
        source.status = "running"

    def capture(*_args, **_kwargs):
        assert source.status in {"paused", "exited", "created"}
        return iter([archive_bytes()]), {}

    source.pause = Mock(side_effect=pause)
    source.unpause = Mock(side_effect=unpause)
    source.get_archive = Mock(side_effect=capture)
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    return WorkspaceUpgrade(pool, tmp_path), source, client


@pytest.mark.parametrize("status", ["running", "paused", "exited", "created"])
def test_backup_restores_the_original_state_and_publishes_a_verified_archive(environment, status):
    upgrade, source, _ = environment
    source.status = status
    name = upgrade.backup("desktop")
    path, metadata, manifest = upgrade.backups.verified(name)
    assert path.is_file() and metadata["source_id"] == source.id
    assert manifest["売上.txt"]["size"] == 5
    assert source.status == status
    assert not source.removed and not source.stopped
    assert source.pause.call_count == source.unpause.call_count == (1 if status == "running" else 0)


@pytest.mark.parametrize("fault", [ReadTimeout("pause response lost"), KeyboardInterrupt()])
def test_an_uncertain_pause_is_released_before_propagating_the_original_failure(environment, fault):
    upgrade, source, _ = environment

    def pause():
        source.status = "paused"
        raise fault

    source.pause.side_effect = pause
    with pytest.raises(type(fault)) as raised:
        upgrade.backup("desktop")
    assert source.status == "running", "the failed backup left the desktop frozen"
    assert raised.value is fault
    source.get_archive.assert_not_called()
    assert not list(upgrade.backups.root.glob("*.json"))


def test_a_rejected_pause_preserves_the_original_error_without_attempting_capture(environment):
    upgrade, source, _ = environment
    fault = APIError("pause rejected before changing the guest")
    source.pause.side_effect = fault
    with pytest.raises(APIError) as raised:
        upgrade.backup("desktop")
    assert raised.value is fault and source.status == "running"
    source.get_archive.assert_not_called()


@pytest.mark.parametrize("capture_fails", [False, True])
def test_lost_unpause_acknowledgement_is_reconciled_with_the_actual_guest_state(
    environment, capture_fails
):
    upgrade, source, _ = environment
    fault = StorageError("archive stream failed")
    if capture_fails:
        source.get_archive.side_effect = fault

    def unpause():
        source.status = "running"
        raise ReadTimeout("unpause response lost")

    source.unpause.side_effect = unpause
    if capture_fails:
        with pytest.raises(StorageError) as raised:
            upgrade.backup("desktop")
        assert raised.value is fault
    else:
        name = upgrade.backup("desktop")
        assert upgrade.backups.verified(name)[1]["source_id"] == source.id
    assert source.status == "running"


@pytest.mark.parametrize("fault", [StorageError("archive failed"), KeyboardInterrupt()])
def test_capture_and_resume_failures_both_remain_available_to_the_operator(environment, fault):
    upgrade, source, _ = environment
    source.get_archive.side_effect = fault
    resume_fault = ReadTimeout("cannot resume desktop")
    source.unpause.side_effect = resume_fault
    with pytest.raises(BaseExceptionGroup) as raised:
        upgrade.backup("desktop")
    assert raised.value.exceptions == (fault, resume_fault)
    assert source.status == "paused"
    assert not list(upgrade.backups.root.glob("*.json"))


@pytest.mark.parametrize("status", ["restarting", "removing", "dead", "unknown"])
def test_unstable_states_are_rejected_before_capturing_a_live_filesystem(environment, status):
    upgrade, source, _ = environment
    source.status = status
    with pytest.raises(DockerUnavailableError, match="状態"):
        upgrade.backup("desktop")
    source.get_archive.assert_not_called()
    source.pause.assert_not_called()
    source.unpause.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "vm", "android"])
def test_missing_or_non_container_environments_are_not_frozen_for_home_backup(environment, kind):
    upgrade, source, _ = environment
    source.attrs["Config"]["Labels"]["com.docker.compose.service"] = kind
    with pytest.raises(ValueError, match="見つかりません"):
        upgrade.backup(kind)
    source.pause.assert_not_called()
    source.get_archive.assert_not_called()


def test_ambiguous_fixed_desktops_are_rejected_before_any_container_is_paused(environment):
    upgrade, source, client = environment
    client.containers.items.append(
        _FakeContainer("duplicate", labels={"com.docker.compose.service": "desktop"})
    )
    with pytest.raises(ValueError, match="1つに絞れません"):
        upgrade.backup("desktop")
    source.pause.assert_not_called()
    source.get_archive.assert_not_called()


@pytest.mark.parametrize("status", ["paused", "unknown", "exited"])
def test_a_failed_resume_keeps_the_verified_backup_and_reports_its_identifier(environment, status):
    upgrade, source, _ = environment
    fault = ReadTimeout("resume response missing")

    def unpause():
        source.status = status
        raise fault

    source.unpause.side_effect = unpause
    with pytest.raises(ReadTimeout) as raised:
        upgrade.backup("desktop")
    assert raised.value is fault
    archives = list(upgrade.backups.root.glob("*.json"))
    assert len(archives) == 1
    name = archives[0].stem
    assert upgrade.backups.verified(name)[1]["source_id"] == source.id
    assert any(name in note for note in fault.__notes__)


def test_failed_resume_and_inspection_keep_the_backup_failure_and_transport_chain(environment):
    upgrade, source, _ = environment
    capture_fault = StorageError("archive failed")
    resume_fault, inspect_fault = ReadTimeout("resume failed"), ReadTimeout("inspect failed")
    source.get_archive.side_effect = capture_fault
    source.unpause.side_effect = resume_fault

    def reload():
        if source.unpause.called:
            raise inspect_fault

    source.reload = Mock(side_effect=reload)
    with pytest.raises(ExceptionGroup) as raised:
        upgrade.backup("desktop")
    assert raised.value.exceptions == (capture_fault, inspect_fault)
    assert inspect_fault.__context__ is resume_fault
    assert source.status == "paused"
