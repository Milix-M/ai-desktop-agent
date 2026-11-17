"""A real copy waiting on a FIFO must not stall unrelated HTTP environment operations."""

import asyncio
import errno
import os
import uuid

import docker
import httpx
import pytest

from ai_desktop_agent.server.networks import LABEL_PROJECT
from tests.integration.test_vm_creations import CreationProject

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


async def test_a_slow_docker_stop_only_holds_its_own_environments_http_operations(tmp_path):
    project = CreationProject(tmp_path)
    stop_requested = project.root / "vm/stop-requested"
    release = project.root / "vm/release-stop"
    stopping = starting = None
    try:
        # PID 1 reports SIGTERM and stays alive until explicitly released by the test.
        project.client.containers.run(
            project.image,
            name=project.project + "-vm-1",
            entrypoint=["python3", "-u", "-c"],
            command=[
                "import signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, lambda *_: Path('/vm/stop-requested').touch()); "
                "print('ready', flush=True); "
                "exec(\"while not Path('/vm/release-stop').exists(): time.sleep(0.02)\")"
            ],
            detach=True,
            network=project.control.name,
            cap_drop=["NET_RAW"],
            labels={**project.labels, "com.docker.compose.service": "vm"},
            volumes={str(project.root / "vm"): {"bind": "/vm", "mode": "rw"}},
        )
        project.client.containers.run(
            project.desktop,
            name=project.project + "-desktop-1",
            detach=True,
            network=project.control.name,
            cap_drop=["NET_RAW"],
            labels={**project.labels, "com.docker.compose.service": "desktop"},
        )
        await project.boot()
        async with project.api() as api:
            for _ in range(150):
                listing = await api.get("/vms")
                assert listing.status_code == 200, listing.text
                if any(v["id"] == "desktop" and v["health"] == "healthy" for v in listing.json()):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("the dedicated VNC desktop never became healthy")
            stopping = asyncio.create_task(api.post("/vms/vm/stop"))
            try:
                for _ in range(100):
                    if stop_requested.exists():
                        break
                    if stopping.done():
                        pytest.fail("stop finished before the controlled SIGTERM wait")
                    await asyncio.sleep(0.02)
                else:
                    pytest.fail("the controlled VM did not receive SIGTERM")
                starting = asyncio.create_task(api.post("/vms/vm/start"))
                task = await api.post(
                    "/tasks", json={"instruction": "画面を確認する", "vm_id": "desktop"}, timeout=3
                )
                assert task.status_code == 200, task.text
                stopped = await api.post("/vms/desktop/stop", timeout=3)
                assert stopped.status_code == 200 and stopped.json()["status"] == "exited"
                assert not stopping.done() and not starting.done()
            finally:
                release.touch()
                assert (await stopping).status_code == 200
                if starting is not None:
                    assert (await starting).status_code == 200
    finally:
        release.touch()
        project.close()


async def test_a_blocked_disk_copy_keeps_other_environment_endpoints_available(tmp_path):
    client = docker.from_env(timeout=15)
    project = "copy-concurrency-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    backend_image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    desktop_image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    runtime_image = os.environ.get("QMP_SMOKE_IMAGE", "ai-desktop-agent-qmp-smoke")
    control = client.networks.create(project + "_default", driver="bridge", labels=labels)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    vm_files = tmp_path / "vm"
    vm_files.mkdir()
    # A FIFO makes the real cp wait for input without allocating a large guest disk.
    template = vm_files / "desktop.qcow2"
    os.mkfifo(template)
    key = "copy-smoke-key-" + uuid.uuid4().hex
    try:
        client.containers.run(
            desktop_image,
            name=project + "-desktop-1",
            detach=True,
            network=control.name,
            cap_drop=["NET_RAW"],
            labels={**labels, "com.docker.compose.service": "desktop"},
        )
        backend = client.containers.run(
            backend_image,
            detach=True,
            network=control.name,
            networking_config={control.name: {"GwPriority": 1}},
            cap_drop=["NET_RAW"],
            labels={**labels, "com.docker.compose.service": "backend"},
            ports={"8081/tcp": ("127.0.0.1", None)},
            environment={
                "COMPOSE_PROJECT_NAME": project,
                "AGENT_ACCESS_TOKEN": key,
                "LLM_PROVIDER": "mock",
                "ANDROID_PROVISION": "0",
                "USE_KVM": "false",
                "ALLOW_TCG_VM": "true",
                "VM_IMAGE_REF": runtime_image,
            },
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                str(data): {"bind": "/app/data", "mode": "rw"},
                str(vm_files): {"bind": "/vm", "mode": "rw"},
            },
        )
        backend.reload()
        port = backend.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + key},
            timeout=15,
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
            creation = asyncio.create_task(api.post("/vms", json={"kind": "qemu"}))
            queued = []
            writer = None
            try:
                # Opening a nonblocking writer succeeds only once cp has opened the reader.
                for _ in range(100):
                    try:
                        writer = os.open(template, os.O_WRONLY | os.O_NONBLOCK)
                        break
                    except OSError as error:
                        if error.errno != errno.ENXIO:
                            raise
                    if creation.done():
                        pytest.fail(
                            f"creation ended before the controlled copy: {creation.result()}"
                        )
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("the real copy did not open the controlled input")
                assert not creation.done()
                # Missing Android input fails after admission, without starting extra guests.
                # One running copy plus eight submissions must reject at least one now.
                queued = [
                    asyncio.create_task(api.post("/vms", json={"kind": "android"}))
                    for _ in range(8)
                ]
                done, _ = await asyncio.wait(queued, timeout=3, return_when=asyncio.FIRST_COMPLETED)
                assert done, "a full creation backlog did not reject another request"
                assert all(task.result().status_code == 429 for task in done)
                assert all(task.result().headers["Retry-After"] for task in done)
                status = await api.get("/vm/status", timeout=3)
                listing = await api.get("/vms", timeout=3)
                stopped = await api.post("/vms/desktop/stop", timeout=3)
                assert status.status_code == listing.status_code == stopped.status_code == 200
                assert stopped.json()["status"] == "exited"
                assert not creation.done(), "copy completed before the input was released"
            finally:
                if writer is not None:
                    # EOF releases cp. The empty output is intentionally not a bootable guest.
                    os.close(writer)
                result = await creation
                queued_results = await asyncio.gather(*queued)
            assert result.status_code == 200, result.text
            assert sorted(response.status_code for response in queued_results) == [400] * 7 + [429]
            assert (await api.delete("/vms/" + result.json()["id"])).status_code == 200
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        for volume in client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            volume.remove()
        # Backend-owned 0700 directories must not prevent pytest's host-user cleanup.
        client.containers.run(
            backend_image,
            entrypoint=["python3", "-c"],
            command=[
                "from pathlib import Path; import shutil; "
                "[(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) "
                "for p in Path('/cleanup').iterdir()]"
            ],
            network_mode="none",
            cap_drop=["NET_RAW"],
            volumes={str(tmp_path): {"bind": "/cleanup", "mode": "rw"}},
            labels=labels,
            remove=True,
        )
        client.close()
