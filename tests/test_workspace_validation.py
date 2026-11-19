"""Reject damaged or misbound home backups before selecting a destination volume."""

import io
import json
import tarfile
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from ai_desktop_agent.server.workspaces import HomeBackups, WorkspaceVolumes
from tests.test_vm_pool import _FakeDocker
from tests.test_workspaces import archive_bytes, source_for


@pytest.fixture
def backup(tmp_path):
    backups = HomeBackups(tmp_path, "ai-desktop-agent")
    name = backups.capture(source_for(archive_bytes()), "desk-sales-team")
    path = backups.root / (name + ".json")
    return backups, name, path


@pytest.mark.parametrize("metadata", [None, [], "damaged metadata", 0, True])
def test_non_object_metadata_is_a_storage_error(backup, metadata):
    backups, name, path = backup
    path.write_text(json.dumps(metadata))
    with pytest.raises(StorageError):
        backups.verified(name)


def test_a_backup_id_cannot_redirect_restore_to_another_environment(backup, tmp_path, monkeypatch):
    backups, name, path = backup
    metadata = json.loads(path.read_text())
    metadata["vm_id"] = "desk-payroll"
    path.write_text(json.dumps(metadata))
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    upgrade = WorkspaceUpgrade(pool, tmp_path)
    ensure = Mock(wraps=upgrade.volumes.ensure)
    monkeypatch.setattr(upgrade.volumes, "ensure", ensure)
    with pytest.raises(StorageError):
        upgrade.restore(name)
    ensure.assert_not_called()
    assert not client.volumes.items and not client.containers.runs
    assert backups.root.joinpath(name + ".tar").is_file()


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", True),
        ("source_id", None),
        ("source_id", ""),
        ("bytes", "10240"),
        ("bytes", True),
        ("bytes", -1),
        ("bytes", 2**60),
        ("sha256", None),
        ("sha256", "not-a-sha256"),
        ("entries", True),
        ("entries", -1),
        ("entries", 999),
        ("entries", 2**60),
        ("created_at", None),
        ("created_at", True),
        ("created_at", -1),
        ("created_at", float("nan")),
        ("created_at", float("inf")),
        pytest.param("created_at", 10**400, id="timestamp-overflow"),
    ],
)
def test_invalid_metadata_fields_cannot_claim_a_verified_backup(backup, field, value):
    backups, name, path = backup
    metadata = json.loads(path.read_text())
    metadata[field] = value
    path.write_text(json.dumps(metadata))
    with pytest.raises(StorageError):
        backups.verified(name)


def test_metadata_requires_the_recorded_fields_but_allows_future_annotations(backup):
    backups, name, path = backup
    metadata = json.loads(path.read_text())
    metadata["annotation"] = "annual report"
    path.write_text(json.dumps(metadata))
    assert backups.verified(name)[1]["annotation"] == "annual report"
    del metadata["source_id"]
    path.write_text(json.dumps(metadata))
    with pytest.raises(StorageError):
        backups.verified(name)


def test_an_archive_that_grows_beyond_its_recorded_size_is_bounded_during_verification(backup):
    backups, name, metadata_path = backup
    metadata = json.loads(metadata_path.read_text())
    backups.max_bytes = metadata["bytes"]
    with metadata_path.with_suffix(".tar").open("ab") as archive:
        archive.write(b"x")
    with pytest.raises(StorageError, match="上限"):
        backups.verified(name)


def test_invalid_backup_names_are_rejected_before_opening_a_path(backup):
    backups, _, _ = backup
    with pytest.raises(ValueError, match="識別子"):
        backups.verified("../other-project/desktop")


@pytest.mark.parametrize("project", ["../outside", "/absolute", "x" * 129])
def test_invalid_project_names_never_select_a_home_directory(tmp_path, project):
    with pytest.raises(ValueError):
        HomeBackups(tmp_path, project)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limits", [{"max_bytes": 0}, {"max_files": 0}])
def test_nonpositive_backup_limits_are_rejected_before_writing(tmp_path, limits):
    with pytest.raises(ValueError, match="正数"):
        HomeBackups(tmp_path, "reports", **limits)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fault", ["symlink-parent", "missing-root", "oversized-member"])
def test_unsafe_manifests_cannot_be_used_for_restore(tmp_path, fault):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        if fault != "missing-root":
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
        if fault == "symlink-parent":
            link = tarfile.TarInfo("bridge")
            link.type, link.linkname = tarfile.SYMTYPE, "/"
            archive.addfile(link)
        member = tarfile.TarInfo("bridge/report" if fault == "symlink-parent" else "report")
        data = b"x" * (2048 if fault == "oversized-member" else 1)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    backups = HomeBackups(tmp_path, "reports", max_bytes=1024)
    output.seek(0)
    with pytest.raises(StorageError):
        backups.manifest(output)


@pytest.mark.parametrize("fault", ["project", "volume-name", "ownership"])
def test_seed_rejects_a_different_destination_before_creating_a_writable_helper(
    backup, fault, monkeypatch
):
    backups, name, _ = backup
    client = _FakeDocker()
    volumes = WorkspaceVolumes(client, "other" if fault == "project" else backups.project)
    volume = volumes.ensure("different" if fault == "volume-name" else "desk-sales-team")
    if fault == "ownership":
        volume.attrs["Labels"]["ai-desktop-agent.project"] = "other"
    helper = Mock(wraps=volumes.helper)
    monkeypatch.setattr(volumes, "helper", helper)
    with pytest.raises(DockerUnavailableError, match="所属"):
        backups.seed(volumes, volume, name)
    helper.assert_not_called()
    assert not client.containers.runs and volume.name in client.volumes.items
