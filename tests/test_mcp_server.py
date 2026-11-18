import asyncio
import socket
import sys
from contextlib import suppress

import pytest
import uvicorn

from ai_desktop_agent.agent.output import TaskOutput
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from tests.http import TEST_ACCESS_KEY


async def test_stdio_client_can_start_and_read_a_task(tmp_path, monkeypatch):
    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import stdio_client

    store = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    submissions = []

    async def submit(request, claim):
        submissions.append((request, claim))
        store.save(
            TaskRecord(
                id=claim.task_id,
                instruction=request.instruction,
                state="completed",
                vm_id=request.vm_id,
                success=True,
                result=[TaskOutput(summary="発送待ち0件", data={"count": 0})],
                request_digest=claim.digest,
                request_fingerprint=claim.fingerprint,
            )
        )
        return server._stored_status(store.load(claim.task_id))

    monkeypatch.setattr(server, "_create_new_task", submit)
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            server.app, lifespan="off", ws="none", log_level="warning", proxy_headers=False
        )
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        serve = asyncio.create_task(uvicorn_server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not uvicorn_server.started:
                    await asyncio.sleep(0.01)
                params = mcp.StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "ai_desktop_agent.mcp_server"],
                    env={
                        "DESKTOP_AGENT_API_URL": f"http://127.0.0.1:{port}",
                        "AGENT_ACCESS_TOKEN": TEST_ACCESS_KEY,
                    },
                )
                async with (
                    stdio_client(params) as (reader, writer),
                    mcp.ClientSession(reader, writer) as client,
                ):
                    await client.initialize()
                    listing = await client.list_tools()
                    assert {tool.name for tool in listing.tools} == {
                        "start_desktop_task",
                        "get_desktop_task",
                    }
                    arguments = {
                        "instruction": "発送待ちの件数を返す",
                        "request_id": "mcp-retry-00000000001",
                        "vm_id": "desktop",
                    }
                    first = await client.call_tool("start_desktop_task", arguments)
                    assert not first.is_error, first
                    task_id = first.structured_content["task_id"]
                    again = await client.call_tool("start_desktop_task", arguments)
                    assert again.structured_content["task_id"] == task_id
                    assert again.structured_content["replayed"] is True
                    result = await client.call_tool("get_desktop_task", {"task_id": task_id})
                    missing = await client.call_tool("get_desktop_task", {"task_id": "missing"})
                    assert missing.is_error and "HTTP 404" in missing.content[0].text
                    assert result.structured_content["result"] == [
                        {"summary": "発送待ち0件", "data": {"count": 0}}
                    ]
        finally:
            uvicorn_server.should_exit = True
            with suppress(asyncio.CancelledError):
                await serve
    assert len(submissions) == 1
    assert submissions[0][0].replace_running is False
