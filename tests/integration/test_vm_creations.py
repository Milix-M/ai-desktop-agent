"""Durable creation receipts across response loss and a real backend replacement."""

import asyncio
import errno
import os
import uuid

import docker
import httpx
import pytest

from ai_desktop_agent.server.networks import LABEL_PROJECT, LABEL_VM_ID

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


class CreationProject:
    def __init__(self, root):
        self.root = root
        self.client = docker.from_env(timeout=15)
        self.project = "creation-jobs-smoke-" + uuid.uuid4().hex[:10]
        self.labels = {LABEL_PROJECT: self.project, "com.docker.compose.project": self.project}
        self.image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
        self.desktop = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
        self.runtime = os.environ.get("QMP_SMOKE_IMAGE", "ai-desktop-agent-qmp-smoke")
        self.control = self.client.networks.create(
            self.project + "_default", driver="bridge", labels=self.labels
        )
        (root / "data").mkdir(mode=0o700)
        (root / "vm").mkdir()
        self.key = "creation-smoke-key-" + uuid.uuid4().hex
        self.backend = None
        self.url = ""

    def spawn_backend(self):
        return self.client.containers.run(
            self.image,
            detach=True,
            network=self.control.name,
            networking_config={self.control.name: {"GwPriority": 1}},
            cap_drop=["NET_RAW"],
            labels={**self.labels, "com.docker.compose.service": "backend"},
            ports={"8081/tcp": ("127.0.0.1", None)},
            environment={
                "COMPOSE_PROJECT_NAME": self.project,
                "AGENT_ACCESS_TOKEN": self.key,
                "LLM_PROVIDER": "mock",
                "ANDROID_PROVISION": "0",
                "USE_KVM": "false",
                "ALLOW_TCG_VM": "true",
                "VM_IMAGE_REF": self.runtime,
                "DESKTOP_IMAGE_REF": self.desktop,
            },
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                str(self.root / "data"): {"bind": "/app/data", "mode": "rw"},
                str(self.root / "vm"): {"bind": "/vm", "mode": "rw"},
            },
        )

    async def boot(self):
        self.backend = self.spawn_backend()
        self.backend.reload()
        port = self.backend.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"]
        self.url = f"http://127.0.0.1:{port}"
        async with self.api() as api:
            for _ in range(150):
                try:
                    if (await api.get("/health")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                self.backend.reload()
                if self.backend.status == "exited":
                    break
                await asyncio.sleep(0.1)
        pytest.fail(self.backend.logs().decode(errors="replace"))

    def api(self):
        return httpx.AsyncClient(
            base_url=self.url, headers={"Authorization": "Bearer " + self.key}, timeout=15
        )

    async def replace_backend(self):
        self.backend.remove(force=True)
        await self.boot()

    def targets(self, vm_id):
        return self.client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_PROJECT}={self.project}", f"{LABEL_VM_ID}={vm_id}"]},
        )

    async def writer_for_copy(self):
        path = self.root / "vm/desktop.qcow2"
        for _ in range(150):
            try:
                return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
            await asyncio.sleep(0.05)
        pytest.fail("the controlled copy never opened the FIFO")

    def close(self):
        try:
            for container in self.client.containers.list(
                all=True, filters={"label": f"{LABEL_PROJECT}={self.project}"}
            ):
                container.remove(force=True)
            for network in self.client.networks.list(
                filters={"label": f"{LABEL_PROJECT}={self.project}"}
            ):
                network.remove()
            for volume in self.client.volumes.list(
                filters={"label": f"{LABEL_PROJECT}={self.project}"}
            ):
                volume.remove()
            self.client.containers.run(
                self.image,
                entrypoint=["python3", "-c"],
                command=[
                    "from pathlib import Path; import shutil; "
                    "[(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) "
                    "for p in Path('/cleanup').iterdir()]"
                ],
                network_mode="none",
                cap_drop=["NET_RAW"],
                volumes={str(self.root): {"bind": "/cleanup", "mode": "rw"}},
                labels=self.labels,
                remove=True,
            )
        finally:
            self.client.close()


@pytest.fixture
async def project(tmp_path):
    project = CreationProject(tmp_path)
    try:
        await project.boot()
        yield project
    finally:
        project.close()


async def completed(api, job_id):
    for _ in range(150):
        response = await api.get("/vm-creations/" + job_id)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["state"] in {"succeeded", "failed"}:
            return record
        await asyncio.sleep(0.1)
    pytest.fail("creation did not reach a terminal state")


async def test_response_loss_and_backend_replacement_do_not_duplicate_a_created_desktop(project):
    headers = {"Idempotency-Key": "retry-desktop-" + uuid.uuid4().hex}
    payload = {"kind": "container", "name": "保存する作業環境"}
    async with project.api() as api:
        accepted = await api.post("/vm-creations", json=payload, headers=headers)
        assert accepted.status_code == 202, accepted.text
        # Do not use the first outcome: a caller may lose it after the server accepted it.
        replay = await api.post("/vm-creations", json=payload, headers=headers)
        assert replay.status_code in (200, 202), replay.text
        assert replay.json()["id"] == accepted.json()["id"]
        result = await completed(api, replay.json()["id"])
        assert result["state"] == "succeeded", result
        vm_id = result["vm_id"]
        assert len(project.targets(vm_id)) == 1
    await project.replace_backend()
    async with project.api() as api:
        replay = await api.post("/vm-creations", json=payload, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["vm_id"] == vm_id
        assert len(project.targets(vm_id)) == 1
        assert (await api.delete("/vms/" + vm_id)).status_code == 200
        replay = await api.post("/vm-creations", json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()["vm_id"] == vm_id
        assert project.targets(vm_id) == []


async def test_a_crashed_copy_is_cleaned_up_without_replaying_the_creation(project):
    os.mkfifo(project.root / "vm/desktop.qcow2")
    headers = {"Idempotency-Key": "retry-copy-" + uuid.uuid4().hex}
    payload = {"kind": "qemu", "name": "中断する環境"}
    async with project.api() as api:
        accepted = await api.post("/vm-creations", json=payload, headers=headers)
        assert accepted.status_code == 202, accepted.text
        job_id, vm_id = accepted.json()["id"], accepted.json()["vm_id"]
    writer = await project.writer_for_copy()
    try:
        assert len(project.targets(vm_id)) == 1  # the owned copy helper, not a guest
        await project.replace_backend()
        async with project.api() as api:
            result = await completed(api, job_id)
            assert result["state"] == "failed" and result["error_status"] == 503, result
            assert project.targets(vm_id) == []
            replay = await api.post("/vm-creations", json=payload, headers=headers)
            assert replay.status_code == 200 and replay.json()["state"] == "failed"
            assert project.targets(vm_id) == []
    finally:
        os.close(writer)


async def test_duplicate_backend_cannot_rewrite_live_history_or_attach_to_desktops(project):
    async with project.api() as api:
        accepted = await api.post(
            "/vm-creations",
            json={"kind": "container", "name": "二重起動の検査"},
            headers={"Idempotency-Key": "ownership-" + uuid.uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        result = await completed(api, accepted.json()["id"])
        assert result["state"] == "succeeded", result

    seeded = project.backend.exec_run(
        [
            "/app/.venv/bin/python",
            "-c",
            "from ai_desktop_agent.server.store import TaskRecord, TaskStore; "
            "store=TaskStore(); "
            "store.save(TaskRecord(id='owner-active', instruction='save report', "
            "state='executing'))",
        ]
    )
    assert seeded.exit_code == 0, seeded.output
    async with project.api() as api:
        before = (await api.get("/tasks/owner-active")).json()
        assert before["state"] == "executing"

    duplicate = project.spawn_backend()
    result = await asyncio.to_thread(duplicate.wait, timeout=20)
    assert result["StatusCode"] != 0
    assert "同じ保存先" in duplicate.logs().decode(errors="replace")
    duplicate.reload()
    assert set(duplicate.attrs["NetworkSettings"]["Networks"]) == {project.control.name}
    async with project.api() as api:
        assert (await api.get("/health")).status_code == 200
        assert (await api.get("/tasks/owner-active")).json() == before

    # SIGKILL releases the kernel lease even though its lock file remains on disk.
    await project.replace_backend()
    async with project.api() as api:
        restored = (await api.get("/tasks/owner-active")).json()
        assert restored["state"] == "interrupted"
        assert restored["success"] is False
        assert restored["instruction"] == before["instruction"]
