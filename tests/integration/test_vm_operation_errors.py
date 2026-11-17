"""Verify primary-container selection and restart with an isolated Docker project."""

import os
import uuid

import docker
import pytest

from ai_desktop_agent.server.vm_control import VmController
from ai_desktop_agent.server.vm_pool import VmPool

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


def test_status_and_debug_restart_use_the_same_compose_container(tmp_path, monkeypatch):
    client = docker.from_env(timeout=15)
    project = "vm-selection-" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    monkeypatch.delenv("VM_CONTAINER_NAME", raising=False)
    labels = {"com.docker.compose.project": project, "com.docker.compose.service": "vm"}
    image = os.environ.get("QMP_SMOKE_IMAGE", "ai-desktop-agent-qmp-smoke")
    created = []
    try:
        for name in (project + "-vm-1", project + "-shadow"):
            created.append(
                client.containers.run(
                    image,
                    name=name,
                    entrypoint=["sleep"],
                    command=["300"],
                    detach=True,
                    network_mode="none",
                    cap_drop=["ALL"],
                    labels=labels,
                )
            )
        main, shadow = created
        main.reload()
        shadow.reload()
        before = [c.attrs["State"]["StartedAt"] for c in created]
        pool = VmPool(client=client, state_file=tmp_path / "vms.json")
        controller = VmController(client=client)
        monkeypatch.setattr(controller, "_qmp_status", lambda: None)
        assert pool.get_vm("vm").vnc_host == main.name
        assert controller.status().name == main.name
        assert controller.restart(timeout=1).name == main.name
        main.reload()
        shadow.reload()
        assert main.attrs["State"]["StartedAt"] != before[0]
        assert shadow.attrs["State"]["StartedAt"] == before[1]
        assert main.status == shadow.status == "running"
    finally:
        for container in created:
            container.remove(force=True)
        client.close()
