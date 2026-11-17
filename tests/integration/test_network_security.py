"""Verify isolation with real bridges, TCP connections, and backend recreation."""

import asyncio
import os
import uuid

import docker
import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ai_desktop_agent.server.auth import COOKIE_NAME
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
                "WEB_CONCURRENCY": "3",
                "DESKTOP_IMAGE_REF": desktop_image,
                "DESKTOP_MEMORY_MB": "3072",
                "DESKTOP_CPUS": "1.5",
                "DESKTOP_PIDS_LIMIT": "768",
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
        async with httpx.AsyncClient(base_url=url, timeout=10) as browser:
            assert (await browser.post("/auth/logout")).status_code == 200
            assert (await browser.get("/auth/status")).json() == {"authenticated": False}
            assert (await browser.post("/auth/login", json={"key": key})).status_code == 200
            assert (await browser.get("/auth/status")).json() == {"authenticated": True}
            stale_cookie = browser.cookies.get(COOKIE_NAME)
            assert stale_cookie
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
            for guest in guests:
                host = guest.attrs["HostConfig"]
                assert host["Memory"] == host["MemorySwap"] == 3072 * 1024**2
                assert host["NanoCpus"] == 1_500_000_000
                assert host["PidsLimit"] == 768
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
            retry_headers = {"Idempotency-Key": "network-smoke-retry-0000000000001"}
            response = await api.post(
                "/tasks",
                json={"instruction": "画面を確認する", "vm_id": created[0]["id"]},
                headers=retry_headers,
            )
            response.raise_for_status()
            task_id = response.json()["session_id"]
            first_order = response.json()["session_order"]
            first_server = response.json()["server_id"]
            assert first_order > 0
            for _ in range(150):
                result = await api.get("/tasks/" + task_id)
                if result.status_code == 200 and result.json()["state"] == "failed":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Real VNC task did not stop at its deadline")
            assert result.json()["stop_reason"] == "duration_limit"
            assert result.json()["error_message"]
            assert result.json()["session_order"] == first_order
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
        async with httpx.AsyncClient(base_url=url, timeout=10) as browser:
            ended = await browser.post(
                "/auth/logout", headers={"Cookie": f"{COOKIE_NAME}={stale_cookie}"}
            )
            assert ended.status_code == 200 and "Max-Age=0" in ended.headers["set-cookie"]
            assert (await browser.post("/auth/logout")).status_code == 200
            assert (await browser.get("/auth/status")).json() == {"authenticated": False}
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=30) as api:
            task = (await api.get("/tasks/" + task_id)).json()
            assert task["state"] == "failed" and task["stop_reason"] == "duration_limit"
            assert task["session_order"] == first_order
            replay = await api.post(
                "/tasks",
                json={"instruction": "画面を確認する", "vm_id": created[0]["id"]},
                headers=retry_headers,
            )
            replay.raise_for_status()
            assert replay.json()["session_id"] == task_id
            assert replay.json()["session_order"] == first_order
            assert replay.json()["replayed"] is True
            restored_server = replay.json()["server_id"]
            assert restored_server != first_server
            assert (await api.get("/tasks/current")).json()["server_id"] == restored_server
            assert len((await api.get("/tasks")).json()) == 1
            async with connect(
                url.replace("http:", "ws:") + "/ws", additional_headers=headers, proxy=None
            ) as ws:
                import json

                snapshot = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert snapshot["server_id"] == restored_server
                assert all(task["server_id"] == restored_server for task in snapshot["tasks"])
                assert snapshot["through_session_order"] >= first_order
                restored = next(t for t in snapshot["tasks"] if t["session_id"] == task_id)
                assert restored["session_order"] == first_order
                await ws.send("ping")
                assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"type": "pong"}
                await ws.send("unsupported")
                with pytest.raises(ConnectionClosed) as ended:
                    await asyncio.wait_for(ws.recv(), 5)
                assert ended.value.rcvd.code == 1003
            for payload in ("x" * (1024 * 1024 + 1), ["😀" * 131073, "😀" * 131073]):
                async with connect(
                    url.replace("http:", "ws:") + "/ws", additional_headers=headers, proxy=None
                ) as ws:
                    assert json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] == "snapshot"
                    await ws.send(payload)
                    with pytest.raises(ConnectionClosed) as ended:
                        await asyncio.wait_for(ws.recv(), 5)
                    assert ended.value.rcvd.code == 1009

            # A raw peer neither acknowledges the close frame nor closes its TCP socket.
            # The backend must finish closing without relying on that cooperation.
            from urllib.parse import urlsplit

            address = urlsplit(url)
            reader, writer = await asyncio.open_connection(address.hostname, address.port)
            try:
                writer.write(
                    (
                        "GET /ws HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        f"Authorization: Bearer {key}\r\n\r\n"
                    ).encode()
                )
                await writer.drain()
                assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).startswith(
                    b"HTTP/1.1 101 "
                )
                mask = b"mask"
                text = b"unsupported"
                frame = bytes(value ^ mask[i % 4] for i, value in enumerate(text))
                writer.write(bytes([0x81, 0x80 | len(text)]) + mask + frame)
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 5)
            finally:
                writer.close()
                await writer.wait_closed()

            async def backend_id():
                async with httpx.AsyncClient(base_url=url, headers=headers, timeout=10) as other:
                    return (await other.get("/tasks/current")).json()["server_id"]

            assert set(await asyncio.gather(*(backend_id() for _ in range(12)))) == {
                restored_server
            }
            response = await api.post(
                "/tasks", json={"instruction": "画面を確認する", "vm_id": created[0]["id"]}
            )
            response.raise_for_status()
            assert response.json()["session_order"] > first_order
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

            # Reject large and stalled uploads over real TCP before login can issue a Cookie.
            async def reject_body(headers, body, status):
                from urllib.parse import urlsplit

                address = urlsplit(url)
                reader, writer = await asyncio.open_connection(address.hostname, address.port)
                try:
                    writer.write(
                        b"POST /auth/login HTTP/1.1\r\nHost: test\r\n" + headers + b"\r\n" + body
                    )
                    await writer.drain()
                    assert (await asyncio.wait_for(api.get("/health"), 1)).status_code == 200
                    reply = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 7)
                    assert reply.startswith(f"HTTP/1.1 {status} ".encode()), reply
                    assert b"connection: close" in reply.lower()
                    assert b"set-cookie:" not in reply.lower()
                    await asyncio.wait_for(reader.read(), 2)
                finally:
                    writer.close()
                    await writer.wait_closed()

            await reject_body(b"Content-Length: 262145\r\nExpect: 100-continue\r\n", b"", 413)
            chunk = b"2000\r\n" + b"x" * 8192 + b"\r\n"
            await reject_body(b"Transfer-Encoding: chunked\r\n", chunk * 33 + b"0\r\n\r\n", 413)
            await reject_body(b"Content-Length: 100\r\n", b'{"key":', 408)
        # A live browser session remains usable while further key guesses are throttled.
        async with httpx.AsyncClient(base_url=url, timeout=10) as browser:
            assert (await browser.post("/auth/login", json={"key": key})).status_code == 200
            for attempt in range(10):
                if attempt % 2 == 0:
                    assert (
                        await browser.post("/auth/login", json={"key": "incorrect"})
                    ).status_code == 401
                else:
                    with pytest.raises(InvalidStatus) as rejected:
                        async with connect(
                            url.replace("http:", "ws:") + "/ws",
                            additional_headers={"Authorization": "Bearer incorrect"},
                            proxy=None,
                        ):
                            pytest.fail("Invalid WebSocket key was accepted")
                    assert rejected.value.response.status_code == 403
            blocked = await browser.post("/auth/login", json={"key": key})
            assert blocked.status_code == 429
            assert 1 <= int(blocked.headers["retry-after"]) <= 60
            assert blocked.headers["cache-control"] == "no-store"
            assert (await browser.get("/auth/status")).json() == {"authenticated": True}
            assert (await browser.get("/tasks")).status_code == 200
            with pytest.raises(InvalidStatus) as rejected:
                async with connect(
                    url.replace("http:", "ws:") + "/ws", additional_headers=headers, proxy=None
                ):
                    pytest.fail("A key was verified during throttling")
            assert rejected.value.response.status_code == 403
            async with connect(
                url.replace("http:", "ws:") + "/ws",
                additional_headers={"Cookie": f"{COOKIE_NAME}={browser.cookies.get(COOKIE_NAME)}"},
                proxy=None,
            ) as ws:
                assert json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] == "snapshot"
                assert (await browser.post("/auth/logout")).status_code == 200
                with pytest.raises(ConnectionClosed) as ended:
                    await asyncio.wait_for(ws.recv(), 5)
                assert ended.value.rcvd.code == 1008
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=10) as api:
            assert (await api.get("/tasks")).status_code == 429
        assert client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}) == [control]
        assert client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}) == [data]
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        for volume in client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            volume.remove()
        client.close()
