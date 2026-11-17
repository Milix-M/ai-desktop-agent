"""Verify isolation with real bridges, TCP connections, and backend recreation."""

import asyncio
import os
import uuid

import docker
import httpx
import pytest
from websockets.asyncio.client import connect

from ai_desktop_agent.server.networks import LABEL_PROJECT

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


async def test_each_desktop_is_isolated_and_backend_recreation_restores_access():
    client = docker.from_env(timeout=15)
    project = "network-smoke-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project}
    control = client.networks.create(project + "_default", driver="bridge", labels=labels)
    data = client.volumes.create(project + "_data", labels=labels)
    key = "smoke-only-" + uuid.uuid4().hex
    backend_image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    desktop_image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    backend = None

    def start_backend():
        return client.containers.run(
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
                "DESKTOP_IMAGE_REF": desktop_image,
                "TASK_MAX_SECONDS": "0.05",
            },
            volumes={
                data.name: {"bind": "/app/data", "mode": "rw"},
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            },
        )

    async def ready(container):
        container.reload()
        port = int(container.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"])
        url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=url, timeout=2) as api:
            for _ in range(100):
                try:
                    if (await api.get("/health")).status_code == 200:
                        return url
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        pytest.fail(container.logs().decode())

    try:
        backend = start_backend()
        url = await ready(backend)
        headers = {"Authorization": "Bearer " + key}
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=30) as api:
            created = []
            for i in range(2):
                response = await api.post(
                    "/vms", json={"kind": "container", "name": f"隔離の確認{i}"}
                )
                response.raise_for_status()
                created.append(response.json())
            for _ in range(100):
                rows = (await api.get("/vms")).json()
                if all(
                    next(v for v in rows if v["id"] == c["id"]).get("health") == "healthy"
                    for c in created
                ):
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("Desktop VNC did not become healthy")
            guests = [client.containers.get(c["vnc_host"]) for c in created]
            nets = [g.attrs["NetworkSettings"]["Networks"] for g in guests]
            assert all(len(n) == 1 for n in nets) and set(nets[0]).isdisjoint(nets[1])
            assert all(not g.attrs["HostConfig"]["PortBindings"] for g in guests)
            assert all("NET_RAW" in g.attrs["HostConfig"]["CapDrop"] for g in guests)
            backend.reload()
            addresses = backend.attrs["NetworkSettings"]["Networks"]
            management = addresses[control.name]["IPAddress"]
            guest_backend = addresses[next(iter(nets[0]))]["IPAddress"]
            other = next(iter(nets[1].values()))["IPAddress"]
            probe = """import socket,sys
from urllib.request import Request,urlopen
from urllib.error import HTTPError
management,private,other=sys.argv[1:]
key=sys.stdin.read().strip()
for host,port in [(private,8081),(other,5900)]:
    try:
        socket.create_connection((host,port),timeout=1).close()
        raise AssertionError('isolated TCP endpoint was reachable')
    except OSError:pass
try:
    response=urlopen(Request('http://'+management+':8081/vms',headers={'Authorization':'Bearer '+key,'Origin':'http://localhost:3000','X-Forwarded-For':management}),timeout=2)
    raise AssertionError('management API was reachable with a stolen key')
except HTTPError as error:assert error.code==403
except OSError:pass
"""
            # Use stdin for the key; it must not appear in process arguments or URLs.
            import subprocess

            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    guests[0].id,
                    "python3",
                    "-c",
                    probe,
                    management,
                    guest_backend,
                    other,
                ],
                input=key,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            for c in created:
                async with connect(
                    url.replace("http:", "ws:") + "/vnc/" + c["id"],
                    additional_headers=headers,
                    subprotocols=["binary"],
                    proxy=None,
                ) as ws:
                    assert (await asyncio.wait_for(ws.recv(), 5)).startswith(b"RFB ")
            # A real VNC-backed task must stop at its deadline and keep the reason on restart.
            response = await api.post(
                "/tasks", json={"instruction": "画面を確認する", "vm_id": created[0]["id"]}
            )
            response.raise_for_status()
            task_id = response.json()["session_id"]
            for _ in range(150):
                result = await api.get("/tasks/" + task_id)
                if result.status_code == 200 and result.json()["state"] == "failed":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Real VNC task did not stop at its deadline")
            assert result.json()["stop_reason"] == "duration_limit"
            assert result.json()["error_message"]
            # A stopped environment must remain discoverable and must not prevent startup.
            response = await api.post("/vms/" + created[1]["id"] + "/stop")
            response.raise_for_status()
            assert response.json()["status"] == "exited"
            # Simulate a stopped environment created before network isolation existed.
            control.connect(guests[1])
            client.networks.get(next(iter(nets[1]))).disconnect(guests[1])
        backend.remove(force=True)
        backend = start_backend()
        url = await ready(backend)
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=30) as api:
            task = (await api.get("/tasks/" + task_id)).json()
            assert task["state"] == "failed" and task["stop_reason"] == "duration_limit"
            rows = (await api.get("/vms")).json()
            assert {v["id"] for v in rows} == {c["id"] for c in created}
            assert next(v for v in rows if v["id"] == created[1]["id"])["status"] == "exited"
            guests[1].reload()
            assert set(guests[1].attrs["NetworkSettings"]["Networks"]) == set(nets[1])
            response = await api.post("/vms/" + created[1]["id"] + "/start")
            response.raise_for_status()
            for _ in range(100):
                rows = (await api.get("/vms")).json()
                if all(v.get("health") == "healthy" for v in rows):
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("Stopped desktop did not recover after backend recreation")
            for c in created:
                assert next(v for v in rows if v["id"] == c["id"])["name"] == c["name"]
                async with connect(
                    url.replace("http:", "ws:") + "/vnc/" + c["id"],
                    additional_headers=headers,
                    subprotocols=["binary"],
                    proxy=None,
                ) as ws:
                    assert (await asyncio.wait_for(ws.recv(), 5)).startswith(b"RFB ")
                response = await api.delete("/vms/" + c["id"])
                response.raise_for_status()
            assert (await api.get("/vms")).json() == []
        assert client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}) == [control]
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        data.remove(force=True)
        client.close()
