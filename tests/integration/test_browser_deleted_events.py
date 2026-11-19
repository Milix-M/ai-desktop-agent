"""A real legacy HTTP/WS peer must not revive deleted task input in Chromium."""

import asyncio
import base64
import json
import os
import socket

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from ai_desktop_agent.server.vnc_proxy import proxy_vnc
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol
from tests.browser import BROWSER, browser
from tests.integration.test_browser_manual_input import frontend as frontend
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real browser opt-in"),
    pytest.mark.skipif(BROWSER is None, reason="Chromium or Chrome is required"),
]


@pytest.mark.parametrize("kind", ["state", "snapshot"])
async def test_delayed_legacy_task_packet_cannot_restore_deleted_input(
    frontend, desktop, tmp_path, kind
):
    desktop.reload()
    guest = next(iter(desktop.attrs["NetworkSettings"]["Networks"].values()))["IPAddress"]
    vm = {
        "id": "desktop",
        "name": "作業環境",
        "kind": "container",
        "status": "running",
        "health": "healthy",
        "managed": False,
        "vnc_host": guest,
        "vnc_port": 5900,
        "ws_port": 0,
    }
    task = {
        "server_id": "legacy-server",
        "session_id": "legacy-task",
        "vm_id": vm["id"],
        "state": "executing",
        "is_running": True,
        "session_order": 0,
        "revision": 0,
        "action_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "subtasks": [{"id": "report", "description": "削除前のタスク"}],
        "current_subtask_index": 0,
    }
    updates = asyncio.Queue()
    sent = asyncio.Event()
    deleted = False
    requests = []
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[frontend],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/auth/status")
    async def auth_status():
        return {"authenticated": True}

    @app.get("/vms")
    async def environments():
        return [vm]

    @app.get("/vm/status")
    async def vm_status():
        return {"running": True, "status": "running", "health": "healthy", "name": vm["name"]}

    @app.get("/tasks/current")
    async def current():
        return (
            task
            if not deleted
            else {
                **task,
                "session_id": None,
                "state": "idle",
                "is_running": False,
                "subtasks": [],
            }
        )

    @app.get("/tasks")
    async def history():
        return (
            []
            if deleted
            else [
                {
                    "id": task["session_id"],
                    "instruction": "削除する報告",
                    "vm_id": vm["id"],
                    "state": "executing",
                    "success": None,
                    "updated_at": 1700000000,
                    "action_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "session_order": 0,
                }
            ]
        )

    @app.delete("/tasks/{task_id}")
    async def remove(task_id: str):
        nonlocal deleted
        assert task_id == task["session_id"]
        requests.append({"method": "DELETE", "task_id": task_id})
        deleted = True
        return {"status": "deleted"}

    @app.websocket("/vnc/desktop")
    async def desktop_view(ws: WebSocket):
        await proxy_vnc(ws, guest)

    @app.websocket("/ws")
    async def status_socket(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"type": "snapshot", "server_id": "legacy-server", "tasks": [task]})

        async def receive():
            while True:
                if await ws.receive_text() == "ping":
                    await ws.send_json({"type": "pong"})

        async def publish():
            while True:
                packet = await updates.get()
                await ws.send_json(packet)
                sent.set()

        pumps = [asyncio.create_task(receive()), asyncio.create_task(publish())]
        try:
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for pump in done:
                await pump
        except WebSocketDisconnect:
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            log_level="warning",
            ws=AgentWebSocketProtocol,
        )
    )
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    stop_button = (
        "[...document.querySelectorAll('.controls button')]"
        ".find(b => b.textContent.trim() === '停止')"
    )
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async with asyncio.timeout(30):
                while True:
                    try:
                        if api.started and (await client.get(frontend)).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
        async with browser(tmp_path) as page:
            await page.command(
                "Page.addScriptToEvaluateOnNewDocument",
                source=f"""
                const rewrite = value => String(value).replace(':8081/', ':{port}/');
                const fetchOriginal = window.fetch.bind(window);
                window.fetch = (url, init) => fetchOriginal(rewrite(url), init);
                const Socket = window.WebSocket;
                window.WebSocket = new Proxy(Socket, {{ construct(target, args) {{
                    args[0] = rewrite(args[0]);
                    return Reflect.construct(target, args);
                }} }});
            """,
            )
            await page.command("Page.navigate", url=frontend)
            await page.wait("!!document.querySelector('.history-delete')")
            await page.wait(f"({stop_button})?.disabled === false")
            await page.wait("!!document.querySelector('.vnc-screen canvas')")
            await page.evaluate("document.querySelector('.history-delete').click()")
            await page.wait("!document.querySelector('.history-delete')")
            await page.wait(f"({stop_button})?.disabled === true")
            assert deleted and requests == [{"method": "DELETE", "task_id": "legacy-task"}]
            waiting = {
                **task,
                "state": "awaiting_user",
                "manual_input": {
                    "id": "retired-consent",
                    "reason": "削除済みタスクの古い入力待ち",
                },
            }
            packet = (
                {
                    "type": "state",
                    "server_id": "legacy-server",
                    "session_id": "legacy-task",
                    "vm_id": vm["id"],
                    "state": "awaiting_user",
                    "session_order": 0,
                    "revision": 1,
                    "action_count": 0,
                    "subtask_index": 0,
                    "subtask_count": 1,
                    "manual_input": waiting["manual_input"],
                }
                if kind == "state"
                else {
                    "type": "snapshot",
                    "server_id": "legacy-server",
                    "through_session_order": 0,
                    "tasks": [waiting],
                }
            )
            await updates.put(packet)
            await asyncio.wait_for(sent.wait(), 5)
            # Cross several browser rendering turns after the actual wire send.
            await page.evaluate(
                "new Promise(resolve => requestAnimationFrame(() => "
                "requestAnimationFrame(() => setTimeout(resolve, 100))))"
            )
            observed = await page.evaluate(f"""({{
                stopDisabled: ({stop_button})?.disabled,
                inputDisabled: document.getElementById('instruction')?.disabled,
                staleConsent: document.body.textContent.includes('削除済みタスクの古い入力待ち'),
                shareButtons: [...document.querySelectorAll('.controls button')]
                    .filter(b => b.textContent.trim() === '画面を送信して再開').length
            }})""")
            (tmp_path / "deleted-task-state.json").write_text(json.dumps(observed, indent=2))
            (tmp_path / "legacy-packet.json").write_text(json.dumps(packet, indent=2))
            capture = await page.command("Page.captureScreenshot", format="png")
            (tmp_path / "deleted-task-screen.png").write_bytes(base64.b64decode(capture["data"]))
            assert observed == {
                "stopDisabled": True,
                "inputDisabled": False,
                "staleConsent": False,
                "shareButtons": 0,
            }
    finally:
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
