"""The browser can publish its debugger port before the initial page is ready."""

import asyncio
import json
from unittest.mock import Mock

import pytest

from tests.browser import wait_for_debugger


async def test_debugger_waits_for_the_port_file_and_a_ready_page(tmp_path):
    requests = []
    profile = tmp_path / "profile"
    profile.mkdir()
    process = Mock()
    process.poll.return_value = None

    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request.split(b"\r\n", 1)[0])
        if len(requests) == 1:
            status, body = "503 Service Unavailable", b"starting"
        elif len(requests) == 2:
            status, body = "200 OK", b"[]"
        else:
            status = "200 OK"
            body = json.dumps([{"type": "page", "webSocketDebuggerUrl": target}]).encode()
        headers = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        writer.write(headers.encode() + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        target = f"ws://127.0.0.1:{port}/devtools/page/1"
        async with asyncio.timeout(3), asyncio.TaskGroup() as group:
            pending = group.create_task(wait_for_debugger(profile, process))
            await asyncio.sleep(0)
            assert not pending.done()
            (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/1\n")
        assert pending.result() == target
    assert requests == [b"GET /json HTTP/1.1"] * 3


async def test_debugger_reports_a_browser_exit_before_the_port_file_exists(tmp_path):
    process = Mock(returncode=17)
    process.poll.return_value = 17
    (tmp_path / "browser.log").write_text("browser exited during startup")
    async with asyncio.timeout(2):
        with pytest.raises(RuntimeError, match="exited with status 17") as error:
            await wait_for_debugger(tmp_path / "profile", process)
    assert "browser exited during startup" in str(error.value)
