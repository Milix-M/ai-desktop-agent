"""Recover real home volumes after restore-stage and helper-cleanup failures."""

import hashlib
import json
import os
import uuid

import docker
import pytest
from docker.models.containers import Container
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from ai_desktop_agent.server.workspaces import HOME, LABEL_HELPER, LABEL_PROJECT

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    client = docker.from_env(timeout=15)
    project = "restore-smoke-" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    original_remove = Container.remove
    try:
        backend = client.containers.run(
            image,
            entrypoint=["sleep"],
            command=["infinity"],
            detach=True,
            network_mode="none",
            cap_drop=["ALL"],
            labels={
                **labels,
                "com.docker.compose.service": "backend",
                "com.docker.compose.oneoff": "true",
            },
        )
        pool = VmPool(client=client, state_file=tmp_path / "vms.json")
        upgrade = WorkspaceUpgrade(pool, tmp_path, backend=backend)
        volume = upgrade.volumes.ensure("desktop")
        yield upgrade, volume, client, project
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            original_remove(container, force=True)
        for volume in client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            volume.remove()
        client.close()


def write_report(helper, text):
    result = helper.exec_run(
        [
            "python",
            "-c",
            "from pathlib import Path; import sys; "
            "Path('/home/agent/report.txt').write_text(sys.argv[1])",
            text,
        ]
    )
    assert result.exit_code == 0, result.output.decode(errors="replace")


@pytest.mark.parametrize("fragment", ["NaN", r'"\ud800"'], ids=["nan", "invalid-unicode"])
def test_invalid_backup_extension_cannot_replace_the_current_home(home, tmp_path, fragment):
    upgrade, volume, _, _ = home
    before = "current report must remain"
    with upgrade.volumes.helper(volume) as helper:
        write_report(helper, "older archived report")
        name = upgrade.backups.capture(helper, "desktop")
        write_report(helper, before)
    metadata = upgrade.backups.root / (name + ".json")
    archive = metadata.with_suffix(".tar")
    original_archive = hashlib.sha256(archive.read_bytes()).hexdigest()
    damaged = metadata.read_text()[:-1] + ',"future_extension":[{"value":' + fragment + "}]}"
    metadata.write_text(damaged)
    proof = {"fragment": "nan" if fragment == "NaN" else "invalid-unicode", "before": before}
    try:
        with upgrade.transaction():
            upgrade.restore(name)
    except Exception as error:
        proof["error"] = type(error).__name__
    with upgrade.volumes.helper(volume, readonly=True) as helper:
        result = helper.exec_run(["cat", HOME + "/report.txt"])
        assert result.exit_code == 0, result.output.decode(errors="replace")
        proof["after"] = result.output.decode()
    proof.update(
        metadata_unchanged=metadata.read_text() == damaged,
        archive_unchanged=hashlib.sha256(archive.read_bytes()).hexdigest() == original_archive,
        backup_count=len(list(upgrade.backups.root.glob("*.json"))),
    )
    (tmp_path / "backup-extension-proof.json").write_text(json.dumps(proof, indent=2))
    assert proof.get("error") == "StorageError", proof
    assert proof["before"] == proof["after"] and proof["backup_count"] == 1, proof
    assert proof["metadata_unchanged"] and proof["archive_unchanged"], proof


@pytest.mark.parametrize("stage", ["cleanup", "upload", "permissions", "sync", "manifest"])
def test_a_failed_restore_keeps_verified_undo_data_and_can_restore_it(home, monkeypatch, stage):
    upgrade, volume, client, project = home
    before = "current report before restore"
    with upgrade.volumes.helper(volume) as helper:
        write_report(helper, "requested older report")
        wanted = upgrade.backups.capture(helper, "desktop")
        write_report(helper, before)
    exec_run, put_archive, get_archive = (
        Container.exec_run,
        Container.put_archive,
        Container.get_archive,
    )
    injected = False

    def execute(container, command, *args, **kwargs):
        nonlocal injected
        source = " ".join(command)
        selected = (
            (stage == "cleanup" and "root.iterdir()" in source)
            or (stage == "permissions" and "os.chown" in source)
            or (stage == "sync" and command[0] == "sync")
        )
        if selected:
            injected = True
            return exec_run(container, ["python", "-c", "raise SystemExit(23)"])
        return exec_run(container, command, *args, **kwargs)

    def upload(container, *args, **kwargs):
        nonlocal injected
        result = put_archive(container, *args, **kwargs)
        if stage == "upload":
            injected = True
            return False
        return result

    def archive(container, *args, **kwargs):
        nonlocal injected
        container.reload()
        writable = any(
            m.get("Destination") == HOME and m.get("RW") for m in container.attrs.get("Mounts", [])
        )
        if stage == "manifest" and writable:
            injected = True
            result = exec_run(
                container,
                [
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "Path('/home/agent/report.txt').write_text('incomplete restore')",
                ],
            )
            assert result.exit_code == 0
        return get_archive(container, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Container, "exec_run", execute)
        fault.setattr(Container, "put_archive", upload)
        fault.setattr(Container, "get_archive", archive)
        with pytest.raises(StorageError), upgrade.transaction():
            upgrade.restore(wanted)
    assert injected, "the intended restore stage was not reached"
    archives = [p.stem for p in upgrade.backups.root.glob("*.json")]
    assert len(archives) == 2 and wanted in archives
    undo = next(name for name in archives if name != wanted)
    assert (
        upgrade.backups.verified(undo)[2]["report.txt"]["sha256"]
        == hashlib.sha256(before.encode()).hexdigest()
    )
    assert not client.containers.list(
        filters={"label": [f"{LABEL_PROJECT}={project}", f"{LABEL_HELPER}=true"]}
    )
    with upgrade.transaction():
        upgrade.restore(undo)
    with upgrade.volumes.helper(volume, readonly=True) as helper:
        result = helper.exec_run(["cat", HOME + "/report.txt"])
        assert result.exit_code == 0 and result.output == before.encode()


@pytest.mark.parametrize("failure", ["none", "home", "home-and-remove"])
def test_uncertain_helper_removal_preserves_the_home_result_and_identifies_leaks(
    home, monkeypatch, failure
):
    upgrade, volume, client, project = home
    original_remove = Container.remove
    operation_fault = StorageError("home operation failed")
    removal_fault = ReadTimeout("helper removal response lost")
    helper_id = None

    def remove(container, *args, **kwargs):
        container.reload()
        labels = container.labels
        selected = labels.get(LABEL_PROJECT) == project and labels.get(LABEL_HELPER) == "true"
        if selected and failure == "home-and-remove":
            raise removal_fault
        result = original_remove(container, *args, **kwargs)
        if selected:
            raise removal_fault
        return result

    def operation():
        nonlocal helper_id
        with upgrade.volumes.helper(volume) as helper:
            helper_id = helper.id
            write_report(helper, "files remain after helper removal")
            if failure != "none":
                raise operation_fault

    with monkeypatch.context() as fault:
        fault.setattr(Container, "remove", remove)
        if failure == "none":
            operation()
        elif failure == "home":
            with pytest.raises(StorageError) as error:
                operation()
            assert error.value is operation_fault
        else:
            with pytest.raises(ExceptionGroup) as error:
                operation()
            assert error.value.exceptions == (operation_fault, removal_fault)
            assert any(helper_id in note for note in removal_fault.__notes__)
    remaining = client.containers.list(
        all=True, filters={"label": [f"{LABEL_PROJECT}={project}", f"{LABEL_HELPER}=true"]}
    )
    assert [c.id for c in remaining] == ([helper_id] if failure == "home-and-remove" else [])
    for container in remaining:
        original_remove(container, force=True)
    with upgrade.volumes.helper(volume, readonly=True) as helper:
        result = helper.exec_run(["cat", HOME + "/report.txt"])
        assert result.exit_code == 0 and result.output == b"files remain after helper removal"


def test_a_misbound_backup_cannot_replace_another_environments_files(home):
    upgrade, desktop, _, _ = home
    payroll = upgrade.volumes.ensure("desk-payroll")
    with upgrade.volumes.helper(desktop) as helper:
        write_report(helper, "older sales report")
        name = upgrade.backups.capture(helper, "desktop")
        write_report(helper, "current sales report")
    with upgrade.volumes.helper(payroll) as helper:
        write_report(helper, "private payroll records")
    path = upgrade.backups.root / (name + ".json")
    metadata = json.loads(path.read_text())
    metadata["vm_id"] = "desk-payroll"
    path.write_text(json.dumps(metadata))
    rejected = False
    try:
        with upgrade.transaction():
            upgrade.restore(name)
    except StorageError:
        rejected = True
    for volume, expected in [
        (desktop, b"current sales report"),
        (payroll, b"private payroll records"),
    ]:
        with upgrade.volumes.helper(volume, readonly=True) as helper:
            result = helper.exec_run(["cat", HOME + "/report.txt"])
            assert result.exit_code == 0 and result.output == expected
    assert rejected, "backup identity and destination must be checked before changing either home"
    assert len(list(upgrade.backups.root.glob("*.json"))) == 1
