"""A terminal observer can outlive the next task without replacing its snapshot."""

import asyncio
import json
import socket

import pytest
import uvicorn
from websockets.asyncio.client import connect

from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol
from tests.http import AUTH_HEADERS, AuthenticatedClient
from tests.test_snapshot_races import snapshot_server as snapshot_server


@pytest.mark.parametrize("retire", ["replace", "delete"])
async def test_replacement_waits_for_terminal_cleanup_and_reconnects_to_the_new_task(
    snapshot_server, retire
):
    store, _, _ = snapshot_server
    waiting, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sessions = []
    pending = None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    application = uvicorn.Server(
        uvicorn.Config(
            server.app,
            lifespan="off",
            log_level="warning",
            ws=AgentWebSocketProtocol,
        )
    )
    serving = asyncio.create_task(application.serve(sockets=[listener]))

    async def hold_terminal(state, context):
        if state == AgentState.COMPLETED:
            waiting.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A public subscriber may need to release its own resources
                # before cancellation can finish and another task can own the VM.
                cancelled.set()
                await release.wait()
                raise

    async def packet(ws):
        return json.loads(await asyncio.wait_for(ws.recv(), 5))

    async def assert_snapshot(ws, expected, state):
        snapshot = await packet(ws)
        assert snapshot["type"] == "snapshot"
        assert snapshot["through_session_order"] >= expected.session_order
        assert len(snapshot["tasks"]) == 1
        current = snapshot["tasks"][0]
        assert current["vm_id"] == "desktop" and current["session_id"] == expected.id
        assert current["state"] == state and current["is_running"] is True
        assert current["session_order"] == expected.session_order

    try:
        async with asyncio.timeout(5):
            while not application.started:
                await asyncio.sleep(0.01)
        async with AuthenticatedClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as api:

            async def start(instruction):
                result = await api.post(
                    "/tasks", json={"instruction": instruction, "vm_id": "desktop"}
                )
                assert result.status_code == 200, result.text
                # Observe the actual API-created session to register a public
                # callback and release its test model's public response gate.
                session = server._sessions[result.json()["session_id"]]
                sessions.append(session)
                await asyncio.wait_for(session.ready.wait(), 5)
                return session

            old = await start("先の報告書を確認する")
            old.on_state_change(hold_terminal)
            old.finish.set()
            await asyncio.wait_for(waiting.wait(), 5)
            assert store.load(old.id).success is True and old.is_running

            async with connect(
                f"ws://127.0.0.1:{port}/ws",
                additional_headers=AUTH_HEADERS,
                proxy=None,
            ) as ws:
                await assert_snapshot(ws, old, "completed")
                if retire == "delete":
                    pending = asyncio.create_task(api.delete(f"/tasks/{old.id}"))
                else:
                    pending = asyncio.create_task(start("次の報告書を確認する"))
                await asyncio.wait_for(cancelled.wait(), 5)
                assert not pending.done() and len(sessions) == 1
                current = await api.get("/tasks/current", params={"vm_id": "desktop"})
                assert current.status_code == 200
                assert current.json()["session_id"] == old.id and current.json()["is_running"]
                assert store.load(old.id).success is True
                await ws.send("ping")
                assert await packet(ws) == {"type": "pong"}
                release.set()
                result = await asyncio.wait_for(asyncio.shield(pending), 5)
                if retire == "delete":
                    assert result.status_code == 200 and result.json() == {"status": "deleted"}
                    assert store.load(old.id) is None
                    newer = await start("次の報告書を確認する")
                else:
                    newer = result
                assert newer.session_order > old.session_order and newer.is_running
                packets = []
                while not any(
                    item["type"] == "state"
                    and item.get("session_id") == newer.id
                    and item["state"] == "executing"
                    for item in packets
                ):
                    packets.append(await packet(ws))
                complete_at = next(
                    i for i, item in enumerate(packets) if item["type"] == "complete"
                )
                newer_at = next(
                    i for i, item in enumerate(packets) if item.get("session_id") == newer.id
                )
                assert packets[complete_at]["session_id"] == old.id
                assert packets[complete_at]["success"] is True and complete_at < newer_at
                assert all(item.get("session_id") != old.id for item in packets[newer_at:])

            async with connect(
                f"ws://127.0.0.1:{port}/ws",
                additional_headers=AUTH_HEADERS,
                proxy=None,
            ) as ws:
                await assert_snapshot(ws, newer, "executing")
            newer.finish.set()
            assert await asyncio.wait_for(asyncio.shield(newer._task), 5)
            assert store.load(newer.id).success is True
            assert len(store.list(limit=None)) == (1 if retire == "delete" else 2)
    finally:
        release.set()
        for session in sessions:
            session.finish.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        application.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
