"""Real home migration, recreation, and restoration without a host filesystem extract."""

import contextlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import docker
import httpx
import pytest
import yaml

from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from ai_desktop_agent.server.workspaces import HOME, LABEL_PROJECT

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


def test_home_survives_update_and_a_verified_backup_can_restore_it(tmp_path, monkeypatch):
    client = docker.from_env(timeout=30)
    project = "workspace-smoke-" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    backend_image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    desktop_image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")

    def desktop(volumes=None):
        container = client.containers.run(
            desktop_image,
            detach=True,
            network_mode="none",
            hostname=project + "-desktop",
            extra_hosts={project + "-desktop": "127.0.1.1"},
            cap_drop=["NET_RAW"],
            volumes=volumes or {},
            labels={**labels, "com.docker.compose.service": "desktop"},
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            container.reload()
            if container.status == "exited":
                pytest.fail(container.logs().decode())
            ready = container.exec_run(
                ["wmctrl", "-m"],
                user="agent",
                environment={"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"},
            )
            if ready.exit_code == 0:
                return container
            time.sleep(0.1)
        pytest.fail("Desktop did not become ready: " + container.logs().decode())

    try:
        backend = client.containers.run(
            backend_image,
            entrypoint=["sleep"],
            command=["infinity"],
            detach=True,
            network_mode="none",
            labels={
                **labels,
                "com.docker.compose.service": "backend",
                "com.docker.compose.oneoff": "true",
            },
        )
        source = desktop()
        script = """from pathlib import Path
import os
root=Path('/home/agent')
(root/'売上.txt').write_bytes(b'saved report\\x00\\xff')
os.chmod(root/'売上.txt',0o640)
os.link(root/'売上.txt',root/'report-alias')
os.symlink('/tmp',root/'temporary-files')
"""
        result = source.exec_run(["python3", "-c", script], user="agent")
        assert result.exit_code == 0, result.output.decode()
        pool = VmPool(client=client, state_file=tmp_path / "vms.json")
        upgrade = WorkspaceUpgrade(pool, tmp_path, backend=backend)
        with upgrade.transaction():
            upgrade.prepare("container")
            source.reload()
            assert source.status == "exited"
            assert source.id in [
                c.id
                for c in client.containers.list(
                    all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
                )
            ]
            name = pool._load_state("desktop")["last_home_backup"]
            volume_name = pool._load_state("desktop")["home_volume"]
            assert upgrade.backups.verified(name)[1]["source_id"] == source.id
            source.remove()
            current = desktop({volume_name: {"bind": HOME, "mode": "rw"}})
        check = """from pathlib import Path
import os
root=Path('/home/agent')
assert (root/'売上.txt').read_bytes()==b'saved report\\x00\\xff'
assert (root/'売上.txt').stat().st_mode&0o777==0o640
assert (root/'売上.txt').stat().st_uid==os.getuid()
assert (root/'report-alias').stat().st_ino==(root/'売上.txt').stat().st_ino
assert os.readlink(root/'temporary-files')=='/tmp'
"""
        result = current.exec_run(["python3", "-c", check], user="agent")
        assert result.exit_code == 0, result.output.decode()
        current.exec_run(
            [
                "python3",
                "-c",
                "from pathlib import Path; "
                "Path('/home/agent/売上.txt').write_text('changed after update')",
            ],
            user="agent",
        )
        current.remove(force=True)
        with upgrade.transaction():
            upgrade.restore(name)
        restored = desktop({volume_name: {"bind": HOME, "mode": "rw"}})
        result = restored.exec_run(["python3", "-c", check], user="agent")
        assert result.exit_code == 0, result.output.decode()
        assert len(list(upgrade.backups.root.glob("*.json"))) == 2
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            container.remove(force=True)
        for volume in client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            volume.remove()
        client.close()


def test_real_startup_script_preserves_a_legacy_home(tmp_path):
    client = docker.from_env(timeout=15)
    project = "upgrade-smoke-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    backend_image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    desktop_image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    production = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )["services"]
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    key = "local-smoke-key-" + uuid.uuid4().hex
    spec = {
        "name": project,
        "services": {
            "desktop": {
                "image": desktop_image,
                "profiles": ["container"],
                "networks": ["env_desktop"],
                "cap_drop": ["NET_RAW"],
                "volumes": ["home_desktop:/home/agent"],
                "labels": labels,
            },
            "backend": {
                "image": backend_image,
                "networks": production["backend"]["networks"],
                "healthcheck": production["backend"]["healthcheck"],
                "labels": labels,
                "environment": {
                    "COMPOSE_PROJECT_NAME": project,
                    "LLM_PROVIDER": "mock",
                    "ANDROID_PROVISION": "0",
                    "AGENT_ACCESS_TOKEN": key,
                },
                "volumes": [str(data) + ":/app/data", "/var/run/docker.sock:/var/run/docker.sock"],
            },
            "frontend": {
                "image": os.environ.get("FRONTEND_SMOKE_IMAGE", "ai-desktop-agent-frontend-smoke"),
                "healthcheck": production["frontend"]["healthcheck"],
                "labels": labels,
            },
        },
        "networks": {
            "default": {},
            "env_desktop": {
                "labels": {"ai-desktop-agent.vm-id": "desktop", "ai-desktop-agent.isolated": "true"}
            },
        },
        "volumes": {
            "home_desktop": {
                "labels": {
                    "ai-desktop-agent.vm-id": "desktop",
                    "ai-desktop-agent.workspace": "home",
                }
            }
        },
    }
    (tmp_path / "docker-compose.yml").write_text(json.dumps(spec))
    (tmp_path / "docker-compose.yml").chmod(0o600)
    (tmp_path / ".env").write_text("USE_KVM=false\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[2] / "scripts/up.sh", scripts / "up.sh")
    try:
        control = client.networks.create(
            project + "_default", labels={**labels, "com.docker.compose.network": "default"}
        )
        private = client.networks.create(
            project + "_env_desktop",
            labels={
                LABEL_PROJECT: project,
                "ai-desktop-agent.vm-id": "desktop",
                "ai-desktop-agent.isolated": "true",
            },
        )
        source = client.containers.run(
            desktop_image,
            name=project + "-desktop-1",
            network=private.name,
            detach=True,
            labels={
                **labels,
                "com.docker.compose.service": "desktop",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.config-hash": "legacy",
            },
        )
        response = source.exec_run(
            [
                "python3",
                "-c",
                "from pathlib import Path; "
                "Path('/home/agent/report.txt').write_text('saved before upgrade')",
            ],
            user="agent",
        )
        assert response.exit_code == 0, response.output.decode()
        backend = client.containers.run(
            backend_image,
            name=project + "-backend-1",
            network=control.name,
            networking_config={control.name: {"GwPriority": 1}},
            detach=True,
            ports={"8081/tcp": ("127.0.0.1", None)},
            environment=spec["services"]["backend"]["environment"],
            volumes={
                str(data): {"bind": "/app/data", "mode": "rw"},
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            },
            labels={
                **labels,
                "com.docker.compose.service": "backend",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.config-hash": "legacy",
            },
        )
        backend.reload()
        port = backend.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"]
        for _ in range(100):
            try:
                if httpx.get("http://127.0.0.1:" + port + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail(backend.logs().decode())
        failed = subprocess.run(
            ["bash", str(scripts / "up.sh"), "--definitely-invalid-up-option"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert failed.returncode != 0
        original = client.containers.get(project + "-desktop-1")
        assert original.id == source.id and original.status == "running"
        result = original.exec_run(
            [
                "python3",
                "-c",
                "from pathlib import Path; "
                "Path('/home/agent/report.txt').write_text('saved after failed update')",
            ],
            user="agent",
        )
        assert result.exit_code == 0, result.output.decode()
        result = subprocess.run(
            ["bash", str(scripts / "up.sh"), "--force-recreate"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            backend = client.containers.get(project + "-backend-1")
            pytest.fail(result.stderr + backend.logs(tail=40).decode(errors="replace"))
        current = client.containers.get(project + "-desktop-1")
        for service in ("desktop", "backend", "frontend"):
            container = client.containers.get(f"{project}-{service}-1")
            assert container.attrs["State"]["Health"]["Status"] == "healthy"
        assert current.id != source.id
        result = current.exec_run(["cat", "/home/agent/report.txt"], user="agent")
        assert result.exit_code == 0 and result.output == b"saved after failed update"
        # Backups deliberately belong to the backend user (0700/0600), not the host test user.
        backend = client.containers.get(project + "-backend-1")
        result = backend.exec_run(
            [
                "python",
                "-c",
                "from pathlib import Path; import sys; "
                "files=list((Path('/app/data/backups')/sys.argv[1]).glob('*.json')); "
                "assert files; assert all(p.stat().st_mode&0o777==0o600 for p in files)",
                project,
            ]
        )
        assert result.exit_code == 0, result.output.decode()

        # Follow the documented offline CLI procedure, including a pre-restore backup.
        def compose(*arguments):
            completed = subprocess.run(
                ["docker", "compose", *arguments],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return completed.stdout.strip()

        def migration(*arguments):
            return compose(
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "--entrypoint",
                "/app/.venv/bin/python",
                "backend",
                "-m",
                "ai_desktop_agent.server.workspace_migration",
                *arguments,
            )

        result = backend.exec_run(
            [
                "python",
                "-c",
                "from pathlib import Path; import json,sys; "
                "files=(Path('/app/data/backups')/sys.argv[1]).glob('*.json'); "
                "first=min(files,key=lambda p:json.loads(p.read_text())['created_at']); "
                "print(first.stem)",
                project,
            ]
        )
        assert result.exit_code == 0, result.output.decode()
        first_backup = result.output.decode().strip()
        compose("--profile", "container", "stop", "backend", "desktop")
        latest_backup = migration("backup", "desktop")
        assert latest_backup.startswith("desktop-") and latest_backup != first_backup
        compose("--profile", "container", "rm", "-f", "desktop")
        migration("restore", first_backup)
        restarted = subprocess.run(
            ["bash", str(scripts / "up.sh")],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        restored = client.containers.get(project + "-desktop-1")
        result = restored.exec_run(["cat", "/home/agent/report.txt"], user="agent")
        assert result.exit_code == 0 and result.output == b"saved before upgrade"
    finally:
        # Return only this test's private bind mount to its host owner for pytest cleanup.
        cleanup = (
            "import os,sys; root='/cleanup'; uid=int(sys.argv[1]); gid=int(sys.argv[2]); "
            "[(os.lchown(os.path.join(p,n),uid,gid)) for p,ds,fs in os.walk(root) "
            "for n in ds+fs]; os.chown(root,uid,gid)"
        )
        client.containers.run(
            backend_image,
            entrypoint=["python", "-c"],
            command=[cleanup, str(os.getuid()), str(os.getgid())],
            network_mode="none",
            remove=True,
            volumes={str(data): {"bind": "/cleanup", "mode": "rw"}},
            labels=labels,
        )
        for container in client.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={project}"}
        ):
            container.remove(force=True)
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            container.remove(force=True)
        for network in client.networks.list(
            filters={"label": f"{LABEL_PROJECT}={project}"}
        ) + client.networks.list(filters={"label": f"com.docker.compose.project={project}"}):
            with contextlib.suppress(docker.errors.NotFound):
                network.remove()
        for volume in client.volumes.list(
            filters={"label": f"com.docker.compose.project={project}"}
        ):
            volume.remove()
        client.close()
