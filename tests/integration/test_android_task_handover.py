"""Real Docker discovery, VNC tasks and guest setup markers share exclusive ownership."""

import json
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_PROJECT, LABEL_VM_ID
from ai_desktop_agent.server.vm_pool import LABEL_KIND

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker/VNC opt-in"),
]
PROBE = Path(__file__).with_name("android_handover_probe.py").read_text()


@pytest.mark.parametrize("finish", ["stop", "complete"])
def test_real_backend_holds_setup_until_each_tasks_own_lifecycle_finishes(tmp_path, finish):
    client = docker.from_env(timeout=30)
    project = "android-task-handover-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    desktop = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    guests = {}
    backend = None
    try:
        network = client.networks.create(project + "_default", driver="bridge", labels=labels)
        for vm_id in ["and-first", "and-other"]:
            guests[vm_id] = client.containers.run(
                desktop,
                detach=True,
                name=project + "-" + vm_id,
                network=network.name,
                shm_size="1g",
                labels={**labels, LABEL_VM_ID: vm_id, LABEL_KIND: "android"},
            )
        deadline = time.monotonic() + 60
        while True:
            for guest in guests.values():
                guest.reload()
            if all(
                guest.attrs["State"].get("Health", {}).get("Status") == "healthy"
                for guest in guests.values()
            ):
                break
            assert time.monotonic() < deadline, {
                vm_id: guest.logs().decode(errors="replace") for vm_id, guest in guests.items()
            }
            time.sleep(0.1)
        backend = client.containers.run(
            image,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=[PROBE, finish],
            detach=True,
            network=network.name,
            networking_config={network.name: {"GwPriority": 1}},
            labels={**labels, "com.docker.compose.service": "backend"},
            cap_drop=["NET_RAW"],
            environment={
                "COMPOSE_PROJECT_NAME": project,
                "DATA_DIR": "/tmp/handover-data",
                "ANDROID_PROVISION": "0",
                "AGENT_ACCESS_TOKEN": "handover-test-" + uuid.uuid4().hex,
            },
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        result = backend.wait(timeout=90)
        logs = backend.logs().decode(errors="replace")
        (tmp_path / "backend.log").write_text(logs)
        assert result["StatusCode"] == 0, logs
        records = [json.loads(line) for line in logs.splitlines() if line.startswith('{"mode":')]
        assert len(records) == 1
        proof = records[0]
        assert proof["mode"] == finish and proof["closed"]
        assert len(proof["sessions"]) == 2
        expected = (40, 40) if finish == "stop" else (8, 9)
        for vm_id, guest in guests.items():
            counted = guest.exec_run(["cat", "/tmp/android-handover-setup-log"])
            assert counted.exit_code == 0 and counted.output == b"setup\n" * 3, (
                vm_id,
                counted.output,
            )
            pointer = guest.exec_run(
                ["xdotool", "getmouselocation", "--shell"],
                user="agent",
                environment={"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"},
            )
            assert pointer.exit_code == 0, pointer.output
            values = dict(line.split("=", 1) for line in pointer.output.decode().splitlines())
            assert (int(values["X"]), int(values["Y"])) == expected, (vm_id, values)
        (tmp_path / "android-handover-proof.json").write_text(json.dumps(proof, indent=2))
    finally:
        try:
            if backend is not None:
                with suppress(NotFound):
                    (tmp_path / "backend.log").write_text(backend.logs().decode(errors="replace"))
            for container in client.containers.list(
                all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
            ):
                with suppress(NotFound):
                    container.remove(force=True)
            for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
                network.remove()
        finally:
            client.close()
