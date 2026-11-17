"""Real HTTP overload handling, including unread bodies and available stop controls."""

import asyncio
import os
from urllib.parse import urlsplit

import pytest

from tests.integration.test_vm_creations import CreationProject

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


@pytest.fixture
async def backend(tmp_path):
    project = CreationProject(tmp_path)
    try:
        await project.boot()
        yield project
    finally:
        project.close()


async def test_slow_request_bodies_cannot_exhaust_task_admission_or_block_stop(backend):
    target = urlsplit(backend.url)
    requests = []
    headers = (
        "POST /tasks HTTP/1.1\r\nHost: localhost\r\n"
        f"Authorization: Bearer {backend.key}\r\n"
        "Content-Type: application/json\r\nContent-Length: 2\r\n"
        "Expect: 100-continue\r\nConnection: close\r\n\r\n"
    ).encode()
    try:
        for _ in range(9):
            reader, writer = await asyncio.open_connection(target.hostname, target.port)
            requests.append((reader, writer))
            writer.write(headers)
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            if len(requests) <= 8:
                # The server has admitted this request and started waiting for its body.
                assert response.startswith(b"HTTP/1.1 100 Continue"), response
            else:
                assert response.startswith(b"HTTP/1.1 429"), response
                assert b"retry-after: 1\r\n" in response.lower()
                assert b"connection: close\r\n" in response.lower()
                assert await asyncio.wait_for(reader.read(), 2)
        async with backend.api() as api:
            assert (await api.get("/health")).status_code == 200
            assert (await api.post("/tasks/current/stop")).status_code == 200
            assert (await api.get("/tasks")).json() == []
            for reader, writer in requests[:8]:
                writer.write(b"{}")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 2)
                assert response.startswith(b"HTTP/1.1 422"), response
            assert (await api.post("/tasks", json={})).status_code == 422
    finally:
        for _, writer in requests:
            writer.close()
        await asyncio.gather(
            *(writer.wait_closed() for _, writer in requests), return_exceptions=True
        )


async def test_http_rate_limit_still_allows_health_and_stop(backend):
    async with backend.api() as api:
        for _ in range(60):
            assert (await api.post("/tasks", json={})).status_code == 422
        response = await api.post("/tasks", json={})
        assert response.status_code == 429
        assert 1 <= int(response.headers["retry-after"]) <= 60
        assert (await api.get("/health")).status_code == 200
        assert (await api.post("/tasks/current/stop")).status_code == 200
        assert (await api.get("/tasks")).json() == []
