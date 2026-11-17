"""Verify admission and runtime compatibility with Docker, HTTP, and a real VNC desktop."""

import asyncio
import os
import uuid

import docker
import httpx
import pytest
from websockets.asyncio.client import connect

from ai_desktop_agent.server.networks import LABEL_PROJECT
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


def test_an_incompatible_runtime_is_rejected_before_the_disk_is_copied(tmp_path, monkeypatch):
    client = docker.from_env(timeout=15)
    # The desktop image has bash/cp, but does not implement the VM boot-file protocol.
    image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    monkeypatch.setenv("VM_IMAGE_REF", image)
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    (tmp_path / "vm").mkdir()
    template = tmp_path / "vm/desktop.qcow2"
    template.write_bytes(b"must not be copied by an unsupported runtime")
    disks = tmp_path / "vm/overlays"
    pool = VmPool(client=client, state_file=tmp_path / "vms.json", overlays_dir=str(disks))
    try:
        with pytest.raises(DockerUnavailableError, match="VM実行イメージの更新"):
            pool._create_overlay("vm-compatibility")
        assert template.read_bytes() == b"must not be copied by an unsupported runtime"
        assert not (disks / "vm-compatibility/disk.qcow2").exists()
    finally:
        pool._remove_disk("vm-compatibility")
        client.close()


async def test_only_ready_desktops_accept_tasks_while_vnc_remains_available():
    client = docker.from_env(timeout=15)
    project = "readiness-smoke-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project}
    control = client.networks.create(project + "_default", driver="bridge", labels=labels)
    key = "readiness-only-" + uuid.uuid4().hex
    backend_image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    desktop_image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    try:
        backend = client.containers.run(
            backend_image,
            detach=True,
            network=control.name,
            networking_config={control.name: {"GwPriority": 1}},
            cap_drop=["NET_RAW"],
            ports={"8081/tcp": ("127.0.0.1", None)},
            labels={
                **labels,
                "com.docker.compose.project": project,
                "com.docker.compose.service": "backend",
            },
            environment={
                "COMPOSE_PROJECT_NAME": project,
                "AGENT_ACCESS_TOKEN": key,
                "LLM_PROVIDER": "mock",
                "ANDROID_PROVISION": "0",
                "TASK_MAX_SECONDS": "0.05",
            },
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        backend.reload()
        port = backend.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"]
        url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(
            base_url=url, headers={"Authorization": "Bearer " + key}, timeout=10
        ) as api:
            for _ in range(100):
                try:
                    if (await api.get("/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail(backend.logs().decode(errors="replace"))

            guest = client.containers.run(
                desktop_image,
                name=project + "_desktop",
                detach=True,
                network=control.name,
                cap_drop=["NET_RAW"],
                shm_size="1g",
                labels={
                    **labels,
                    "ai-desktop-agent.vm-id": "desk-readiness",
                    "ai-desktop-agent.kind": "container",
                },
                healthcheck={
                    "test": ["CMD-SHELL", "test -e /tmp/task-ready"],
                    "interval": 1_000_000_000,
                    "timeout": 1_000_000_000,
                    "retries": 1,
                    "start_period": 30_000_000_000,
                },
            )
            rows = (await api.get("/vms")).json()
            assert next(v for v in rows if v["id"] == "desk-readiness")["health"] == "starting"
            rejected = await api.post(
                "/tasks", json={"vm_id": "desk-readiness", "instruction": "画面を確認する"}
            )
            assert rejected.status_code == 409, rejected.text
            assert (await api.get("/tasks")).json() == []

            for _ in range(100):
                if guest.exec_run(["nc", "-z", "localhost", "5900"]).exit_code == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("VNC did not become available")
            async with connect(
                url.replace("http:", "ws:") + "/vnc/desk-readiness",
                subprotocols=["binary"],
                additional_headers={"Authorization": "Bearer " + key},
                proxy=None,
            ) as ws:
                greeting = await asyncio.wait_for(ws.recv(), 5)
                assert isinstance(greeting, bytes) and greeting.startswith(b"RFB ")

            for _ in range(160):
                guest.reload()
                if guest.attrs["State"]["Health"]["Status"] == "unhealthy":
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("The controlled health check never failed")
            assert (
                await api.post(
                    "/tasks", json={"vm_id": "desk-readiness", "instruction": "画面を確認する"}
                )
            ).status_code == 409
            assert (await api.get("/tasks")).json() == []
            guest.exec_run(["touch", "/tmp/task-ready"])
            for _ in range(50):
                guest.reload()
                if guest.attrs["State"]["Health"]["Status"] == "healthy":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("The controlled health check did not recover")
            accepted = await api.post(
                "/tasks", json={"vm_id": "desk-readiness", "instruction": "画面を確認する"}
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["session_id"]
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        client.close()
