import io
import json
import tarfile
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from ai_desktop_agent.server.workspaces import HOME, LABEL_HOME, HomeBackups, WorkspaceVolumes
from tests.test_vm_pool import _FakeContainer, _FakeDocker


def archive_bytes(extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        root = tarfile.TarInfo(".")
        root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o750, 1001, 1001
        archive.addfile(root)
        file = tarfile.TarInfo("./売上.txt")
        file.mode, file.uid, file.gid, file.size = 0o640, 1001, 1001, 5
        archive.addfile(file, io.BytesIO(b"sales"))
        if extra is not None:
            archive.addfile(extra)
    return output.getvalue()


def source_for(data):
    return Mock(id="legacy-desktop", get_archive=Mock(return_value=(iter([data]), {})))


def test_backups_are_private_verified_and_keep_file_ownership(tmp_path):
    backups = HomeBackups(tmp_path, "my-project")
    source = source_for(archive_bytes())
    name = backups.capture(source, "desktop")
    path, metadata, entries = backups.verified(name)
    assert metadata["vm_id"] == "desktop" and metadata["source_id"] == source.id
    assert entries["売上.txt"]["uid"] == 1001 and entries["売上.txt"]["mode"] == 0o640
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_suffix(".json").stat().st_mode & 0o777 == 0o600
    source.remove.assert_not_called()


@pytest.mark.parametrize("failure", ["content", "metadata", "project"])
def test_damaged_or_foreign_backup_is_rejected(tmp_path, failure):
    backups = HomeBackups(tmp_path, "my-project")
    name = backups.capture(source_for(archive_bytes()), "desktop")
    path = backups.root / (name + ".tar")
    if failure == "content":
        path.write_bytes(b"damaged")
    else:
        metadata = json.loads(path.with_suffix(".json").read_text())
        if failure == "project":
            metadata["project"] = "other-project"
        else:
            metadata.pop("bytes")
        path.with_suffix(".json").write_text(json.dumps(metadata))
    with pytest.raises(StorageError):
        backups.verified(name)


@pytest.mark.parametrize(
    "kind", ["escape", "absolute", "device", "link-escape", "missing-link", "duplicate"]
)
def test_unsafe_tar_entries_never_reach_a_restoration_helper(tmp_path, kind):
    member = tarfile.TarInfo("extra")
    if kind == "escape":
        member.name = "../outside"
    elif kind == "absolute":
        member.name = "/outside"
    elif kind == "device":
        member.type = tarfile.CHRTYPE
    elif kind in {"link-escape", "missing-link"}:
        member.type = tarfile.LNKTYPE
        member.linkname = "../outside" if kind == "link-escape" else "missing-file"
    else:
        member.name = "売上.txt"
    backups = HomeBackups(tmp_path, "project")
    with pytest.raises(StorageError):
        backups.capture(source_for(archive_bytes(member)), "desktop")
    assert not list(backups.root.glob("*.json"))
    assert not list(backups.root.glob("*.tmp"))


def test_ordinary_symbolic_and_hard_links_are_preserved(tmp_path):
    link = tarfile.TarInfo("alias")
    link.type, link.linkname = tarfile.LNKTYPE, "./売上.txt"
    backups = HomeBackups(tmp_path, "project")
    name = backups.capture(source_for(archive_bytes(link)), "desktop")
    _, _, entries = backups.verified(name)
    assert entries["alias"] == entries["売上.txt"]
    link.type, link.linkname = tarfile.SYMTYPE, "/tmp"
    name = backups.capture(source_for(archive_bytes(link)), "desktop")
    assert backups.verified(name)[2]["alias"]["target"] == "/tmp"


@pytest.mark.parametrize("limits", [{"max_bytes": 50}, {"max_files": 1}])
def test_archive_limits_abort_without_publishing_a_backup(tmp_path, limits):
    backups = HomeBackups(tmp_path, "project", **limits)
    with pytest.raises(StorageError):
        backups.capture(source_for(archive_bytes()), "desktop")
    assert not list(backups.root.iterdir())


@pytest.mark.parametrize("mismatch", ["project", "purpose", "driver", "options", "seed"])
def test_unknown_volume_is_not_adopted_or_deleted(mismatch):
    docker = _FakeDocker()
    manager = WorkspaceVolumes(docker, "ai-desktop-agent")
    volume = manager.ensure("desk-protected", seed_source="original")
    if mismatch == "project":
        volume.attrs["Labels"]["ai-desktop-agent.project"] = "other"
    elif mismatch == "purpose":
        volume.attrs["Labels"][LABEL_HOME] = "other"
    elif mismatch == "driver":
        volume.attrs["Driver"] = "nfs"
    elif mismatch == "options":
        volume.attrs["Options"] = {"type": "none", "device": "/", "o": "bind"}
    with pytest.raises(DockerUnavailableError):
        manager.ensure(
            "desk-protected", seed_source="another" if mismatch == "seed" else "original"
        )
    if mismatch != "seed":
        with pytest.raises(DockerUnavailableError):
            manager.remove("desk-protected")
    assert volume.name in docker.volumes.items


def test_new_desktops_get_distinct_homes_and_delete_only_the_selected_one(tmp_path):
    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    a, b = pool.create_vm(kind="container"), pool.create_vm(kind="container")
    assert len(docker.volumes.items) == 2
    mounts = [docker.containers.get(v.vnc_host).attrs["Mounts"] for v in (a, b)]
    assert mounts[0][0]["Name"] != mounts[1][0]["Name"]
    assert all(m[0]["Destination"] == HOME for m in mounts)
    home = pool._load_state(a.id)["home_volume"]
    pool.remove_vm(a.id)
    assert home not in docker.volumes.items and len(docker.volumes.items) == 1


def test_volume_deletion_failure_preserves_metadata_for_retry(tmp_path, monkeypatch):
    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    created = pool.create_vm(kind="container")
    volume = docker.volumes.get(pool._load_state(created.id)["home_volume"])
    remove = volume.remove
    monkeypatch.setattr(volume, "remove", Mock(side_effect=RuntimeError("in use")))
    with pytest.raises(DockerUnavailableError):
        pool.remove_vm(created.id)
    assert pool._load_state(created.id) is not None
    monkeypatch.setattr(volume, "remove", remove)
    assert pool.remove_vm(created.id) and pool._load_state(created.id) is None


def test_failed_preparation_restarts_original_containers_and_keeps_their_backup(tmp_path):
    docker = _FakeDocker()
    source = _FakeContainer("old-desktop", labels={"com.docker.compose.service": "desktop"})
    backend = _FakeContainer("backend-main", labels={"com.docker.compose.service": "backend"})
    docker.containers.items.extend([source, backend])
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    upgrade = WorkspaceUpgrade(pool, tmp_path)
    source.get_archive = Mock(return_value=(iter([archive_bytes()]), {}))
    upgrade.backups.seed = Mock(side_effect=RuntimeError("copy failed"))
    with pytest.raises(RuntimeError, match="copy failed"), upgrade.transaction():
        upgrade.prepare("container")
    assert source.started and backend.started and not source.removed
    assert len(list(upgrade.backups.root.glob("*.json"))) == 1


def test_duplicate_desktops_fail_before_stopping_any_backend(tmp_path):
    docker = _FakeDocker()
    docker.containers.items.extend(
        [
            _FakeContainer("desktop-one", labels={"com.docker.compose.service": "desktop"}),
            _FakeContainer("desktop-two", labels={"com.docker.compose.service": "desktop"}),
        ]
    )
    upgrade = WorkspaceUpgrade(VmPool(client=docker), tmp_path)
    with pytest.raises(DockerUnavailableError), upgrade.transaction():
        upgrade.prepare("container")
    assert not upgrade.stopped


@pytest.mark.parametrize("kind", ["orphan", "live-owner", "foreign", "extra-mount"])
def test_retry_reclaims_only_an_identified_orphan_helper(kind):
    from ai_desktop_agent.server.workspaces import LABEL_HELPER, LABEL_PROJECT

    docker = _FakeDocker()
    manager = WorkspaceVolumes(docker, "ai-desktop-agent")
    volume = manager.ensure("desktop")
    parent = _FakeContainer("previous-upgrade", status="exited")
    helper = _FakeContainer(
        "copy-helper", labels={LABEL_HELPER: "true", "ai-desktop-agent.helper-owner": parent.id}
    )
    helper.attrs.update(
        {
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True},
            "Mounts": [{"Type": "volume", "Name": volume.name, "Destination": HOME}],
        }
    )
    if kind == "live-owner":
        parent.status = "running"
    elif kind == "foreign":
        helper.attrs["Config"]["Labels"][LABEL_PROJECT] = "other-project"
    elif kind == "extra-mount":
        helper.attrs["Mounts"].append({"Destination": "/other"})
    docker.containers.items.extend([parent, helper])
    manager.reap_helpers()
    assert helper.removed == (kind == "orphan")
    assert volume.name in docker.volumes.items


def test_backup_accepts_the_full_supported_environment_id_length(tmp_path):
    backups = HomeBackups(tmp_path, "project")
    vm_id = "d" * 128
    name = backups.capture(source_for(archive_bytes()), vm_id)
    assert backups.verified(name)[1]["vm_id"] == vm_id


@pytest.mark.parametrize("failure", [None, "create", "foreign-member"])
def test_old_network_labels_are_repaired_and_members_reconnected(tmp_path, monkeypatch, failure):
    from ai_desktop_agent.server.networks import LABEL_ISOLATED, LABEL_PROJECT, LABEL_VM_ID

    docker = _FakeDocker()
    source = _FakeContainer("desktop", labels={"com.docker.compose.service": "desktop"})
    backend = _FakeContainer("backend-main", labels={"com.docker.compose.service": "backend"})
    docker.containers.items.extend([source, backend])
    network = docker.networks.create(
        "ai-desktop-agent_env_desktop",
        labels={LABEL_PROJECT: "ai-desktop-agent", LABEL_VM_ID: "desktop", LABEL_ISOLATED: "true"},
    )
    network.connect(source, aliases=["desktop"])
    network.connect(backend, aliases=["backend"])
    source.status = backend.status = "exited"
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    upgrade = WorkspaceUpgrade(pool, tmp_path)
    if failure == "foreign-member":
        outsider = _FakeContainer("other-project", labels={LABEL_PROJECT: "other"})
        docker.containers.items.append(outsider)
        network.connect(outsider)
    original = docker.networks.create
    if failure == "create":
        calls = 0

        def create(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Docker disconnected")
            return original(*args, **kwargs)

        monkeypatch.setattr(docker.networks, "create", create)
    if failure:
        with pytest.raises((RuntimeError, DockerUnavailableError)):
            upgrade._reconcile_network("desktop")
    else:
        upgrade._reconcile_network("desktop")
    current = docker.networks.get(network.name)
    assert network.name in source.attrs["NetworkSettings"]["Networks"]
    assert network.name in backend.attrs["NetworkSettings"]["Networks"]
    assert source.attrs["NetworkSettings"]["Networks"][network.name]["Aliases"] == ["desktop"]
    if not failure:
        assert current.attrs["Labels"]["com.docker.compose.network"] == "env_desktop"
    if failure == "foreign-member":
        assert current is network and not outsider.removed
