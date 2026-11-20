"""Real Docker freezing and archive streams survive lost acknowledgements."""

import contextlib
import os
import subprocess
import uuid

import docker
import pytest
from docker.models.containers import Container
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from ai_desktop_agent.server.workspaces import LABEL_PROJECT

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


@pytest.mark.parametrize("fault", ["pause-ack", "unpause-ack", "archive", "archive-and-resume"])
def test_backup_reconciles_actual_container_state_and_retains_failure_evidence(
    tmp_path, monkeypatch, fault
):
    client = docker.from_env(timeout=15)
    project = "backup-smoke-" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    original_pause, original_unpause, original_archive = (
        Container.pause,
        Container.unpause,
        Container.get_archive,
    )
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
        source = client.containers.run(
            image,
            name=project + "-desktop-1",
            entrypoint=["sleep"],
            command=["infinity"],
            detach=True,
            network_mode="none",
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "desktop"},
        )
        ready = source.exec_run(
            [
                "/app/.venv/bin/python",
                "-c",
                "from pathlib import Path; root=Path('/home/agent'); root.mkdir(parents=True); "
                "(root/'saved.txt').write_bytes(b'preserved report\\x00\\xff')",
            ]
        )
        assert ready.exit_code == 0, ready.output.decode(errors="replace")

        def pause(container):
            original_pause(container)
            if container.id == source.id and fault == "pause-ack":
                raise ReadTimeout("pause response lost after Docker froze the guest")

        def unpause(container):
            if container.id == source.id and fault == "archive-and-resume":
                raise ReadTimeout("resume request failed while the guest is still frozen")
            original_unpause(container)
            if container.id == source.id and fault == "unpause-ack":
                raise ReadTimeout("unpause response lost after Docker resumed the guest")

        def archive(container, *args, **kwargs):
            chunks, info = original_archive(container, *args, **kwargs)
            if container.id != source.id or fault not in {"archive", "archive-and-resume"}:
                return chunks, info

            def interrupted():
                try:
                    yield next(chunks)
                    raise ReadTimeout("archive stream response lost")
                finally:
                    chunks.close()

            return interrupted(), info

        monkeypatch.setattr(Container, "pause", pause)
        monkeypatch.setattr(Container, "unpause", unpause)
        monkeypatch.setattr(Container, "get_archive", archive)
        pool = VmPool(client=client, state_file=tmp_path / "vms.json")
        upgrade = WorkspaceUpgrade(pool, tmp_path, backend=backend)
        if fault == "pause-ack":
            with pytest.raises(ReadTimeout, match="pause response lost"):
                upgrade.backup("desktop")
        elif fault == "archive":
            with pytest.raises(StorageError) as error:
                upgrade.backup("desktop")
            assert isinstance(error.value.__cause__, ReadTimeout)
        elif fault == "archive-and-resume":
            with pytest.raises(ExceptionGroup) as error:
                upgrade.backup("desktop")
            assert len(error.value.exceptions) == 2
            assert isinstance(error.value.exceptions[0], StorageError)
            assert isinstance(error.value.exceptions[1], ReadTimeout)
        else:
            name = upgrade.backup("desktop")
            path, metadata, entries = upgrade.backups.verified(name)
            assert path.is_file() and metadata["source_id"] == source.id
            assert entries["saved.txt"]["size"] == len(b"preserved report\x00\xff")
        source.reload()
        assert source.status == ("paused" if fault == "archive-and-resume" else "running")
        assert not list(upgrade.backups.root.glob("*.tmp"))
        assert len(list(upgrade.backups.root.glob("*.json"))) == (
            1 if fault == "unpause-ack" else 0
        )
        if fault == "archive-and-resume":
            resumed = subprocess.run(
                ["docker", "unpause", source.id], capture_output=True, text=True, timeout=15
            )
            assert resumed.returncode == 0, resumed.stderr
            source.reload()
            assert source.status == "running"
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            with contextlib.suppress(Exception):
                original_unpause(container)
            container.remove(force=True)
        client.close()
