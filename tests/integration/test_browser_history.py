"""Recover a lost deletion response through the real browser, API and task store."""

import asyncio
import base64
import json
import os
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.browser_access import ALLOWED_ORIGINS
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_pool import VmInfo
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol
from tests.browser import BROWSER, browser
from tests.http import TEST_ACCESS_KEY
from tests.integration.test_browser_manual_input import frontend as frontend

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real browser opt-in"),
    pytest.mark.skipif(BROWSER is None, reason="Chromium or Chrome is required"),
]


@pytest.mark.parametrize("failure", ["lost-response", "invalid-ack", "timeout"])
async def test_lost_history_deletion_is_visible_and_retry_accepts_an_already_removed_record(
    frontend, tmp_path, monkeypatch, failure
):
    store = TaskStore(tmp_path / "data")
    record = TaskRecord(
        id="history-wire-loss",
        instruction="削除応答を失った履歴",
        vm_id="desktop",
        state="completed",
        success=True,
        session_order=1,
    )
    store.save(record)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        server, "_pool", SimpleNamespace(list_vms=lambda: [], isolate_environments=lambda: None)
    )
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    origins = ALLOWED_ORIGINS[:]
    ALLOWED_ORIGINS.append(frontend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stall_next = failure == "timeout"
    disconnected = asyncio.Event()

    async def faulty_api(scope, receive, send):
        nonlocal stall_next
        stall = (
            stall_next
            and scope["type"] == "http"
            and scope["method"] == "DELETE"
            and scope["path"] == f"/tasks/{record.id}"
        )
        if stall:
            stall_next = False

        async def forward(message):
            if stall and message["type"] == "http.response.body":
                # The real store has deleted the record; only the response body stalls.
                await send({**message, "body": message["body"][:1], "more_body": True})
                while (await receive())["type"] != "http.disconnect":
                    pass
                disconnected.set()
                return
            await send(message)

        await server.app(scope, receive, forward)

    api = uvicorn.Server(
        uvicorn.Config(faulty_api, lifespan="off", log_level="warning", ws=AgentWebSocketProtocol)
    )
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async with asyncio.timeout(30):
                while True:
                    try:
                        if (await client.get(frontend)).status_code == 200 and api.started:
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
                let loseResponse = true;
                window.historyDeleteRequests = 0;
                window.fetch = async (url, init) => {{
                    const target = rewrite(url);
                    const deleting = init?.method === 'DELETE'
                        && target.endsWith('/tasks/{record.id}');
                    if (deleting) window.historyDeleteRequests++;
                    const response = await fetchOriginal(target, init);
                    if (deleting && loseResponse && {json.dumps(failure)} !== 'timeout') {{
                        loseResponse = false;
                        await response.arrayBuffer();
                        if ({json.dumps(failure)} === 'lost-response')
                            throw new TypeError('simulated response loss');
                        return new Response('null', {{
                            status: 200, headers: {{'Content-Type': 'application/json'}}
                        }});
                    }}
                    return response;
                }};
                const Socket = window.WebSocket;
                window.WebSocket = new Proxy(Socket, {{ construct(target, args) {{
                    args[0] = rewrite(args[0]);
                    return Reflect.construct(target, args);
                }} }});
                """,
            )
            await page.command("Page.navigate", url=frontend)
            await page.wait("!!document.getElementById('access-key')")
            await page.evaluate("document.getElementById('access-key').focus()")
            await page.command("Input.insertText", text=TEST_ACCESS_KEY)
            await page.evaluate("document.querySelector('button[type=submit]').click()")
            await page.wait("!!document.querySelector('.history-main')")
            await page.evaluate("document.querySelector('.history-main').click()")
            await page.wait("document.body.textContent.includes('タスク: 削除応答を失った履歴')")
            await page.evaluate("document.querySelector('.history-delete').click()")
            expected_error = {
                "lost-response": "simulated response loss",
                "invalid-ack": "操作の応答が不正です",
                "timeout": "応答を確認できません",
            }[failure]
            await page.wait(
                "document.querySelector('[role=alert]')?.textContent"
                + f".includes({json.dumps(expected_error)})",
                timeout=40 if failure == "timeout" else 20,
            )
            if failure == "timeout":
                async with asyncio.timeout(5):
                    await disconnected.wait()
            assert store.load(record.id) is None
            assert await page.evaluate("!!document.querySelector('.history-item.selected')")
            assert await page.evaluate("!document.querySelector('.history-delete').disabled")
            assert await page.evaluate("window.historyDeleteRequests") == 1
            await page.evaluate(
                "document.querySelector('[role=alert]').scrollIntoView({block:'center'})"
            )
            screenshot = await page.command("Page.captureScreenshot", format="png")
            (tmp_path / "history-deletion-error.png").write_bytes(
                base64.b64decode(screenshot["data"])
            )
            await page.evaluate("document.querySelector('.history-delete').click()")
            await page.wait("!document.querySelector('.history-main')")
            assert await page.evaluate("window.historyDeleteRequests") == 2
            assert await page.evaluate("!document.querySelector('[role=alert]')")
            assert await page.evaluate(
                "!document.body.textContent.includes('タスク: 削除応答を失った履歴')"
            )
            assert store.load(record.id) is None
    finally:
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            ALLOWED_ORIGINS[:] = origins


async def test_malformed_http_records_preserve_history_selection_and_the_unsent_draft(
    frontend, tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "data")
    first = TaskRecord(
        id="saved-a",
        instruction="保存済みの売上報告",
        vm_id="desktop",
        state="completed",
        success=True,
        session_order=1,
    )
    second = TaskRecord(
        id="saved-b",
        instruction="別の保存済み報告",
        vm_id="desktop",
        state="completed",
        success=True,
        session_order=2,
    )
    store.save(first)
    store.save(second)
    vm = VmInfo(
        id="desktop",
        name="作業環境",
        status="running",
        health="healthy",
        kind="container",
        vnc_host="desktop",
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        server,
        "_pool",
        SimpleNamespace(
            list_vms=lambda: [vm],
            get_instance=lambda _: None,
            get_vm=lambda _: None,
            isolate_environments=lambda: None,
            prepare_vm=lambda _: None,
        ),
    )
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_default_vm_id", lambda: "desktop")

    @asynccontextmanager
    async def controller():
        yield SimpleNamespace(
            status=lambda: SimpleNamespace(
                running=True, status="running", health="healthy", name="desktop", qmp_status=None
            )
        )

    monkeypatch.setattr(server, "_controller_connection", controller)
    faults = {"detail": "null", "lists": False, "login": True}

    async def faulty_api(scope, receive, send):
        path = scope.get("path")
        fault = (
            "login"
            if faults["login"] and path == "/auth/login"
            else faults["detail"]
            if path == "/tasks/saved-b"
            else "null"
            if faults["lists"] and path in {"/vms", "/tasks"}
            else None
        )
        pending = None

        async def forward(message):
            nonlocal pending
            if scope["type"] == "http" and fault is not None:
                if message["type"] == "http.response.start" and message["status"] == 200:
                    pending = message
                    return
                if message["type"] == "http.response.body" and pending is not None:
                    if fault == "login":
                        body = b'{"authenticated":false}'
                    elif fault == "wrong-id":
                        payload = json.loads(message["body"])
                        payload["id"] = "unrelated-task"
                        body = json.dumps(payload).encode()
                    else:
                        body = b"null"
                    headers = [
                        (key, value)
                        for key, value in pending["headers"]
                        if key.lower() != b"content-length"
                    ]
                    await send(
                        {
                            **pending,
                            "headers": [*headers, (b"content-length", str(len(body)).encode())],
                        }
                    )
                    pending = None
                    await send({**message, "body": body})
                    return
            await send(message)

        await server.app(scope, receive, forward)

    origins = ALLOWED_ORIGINS[:]
    ALLOWED_ORIGINS.append(frontend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(
        uvicorn.Config(faulty_api, lifespan="off", log_level="warning", ws=AgentWebSocketProtocol)
    )
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async with asyncio.timeout(30):
                while True:
                    try:
                        if (await client.get(frontend)).status_code == 200 and api.started:
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
                window.payloadReads = {{}};
                window.fetch = async (url, init) => {{
                    const target = rewrite(url);
                    const response = await fetchOriginal(target, init);
                    const path = new URL(target).pathname;
                    const parse = response.json.bind(response);
                    response.json = async () => {{
                        const value = await parse();
                        window.payloadReads[path] = (window.payloadReads[path] || 0) + 1;
                        return value;
                    }};
                    return response;
                }};
                const Socket = window.WebSocket;
                window.WebSocket = new Proxy(Socket, {{ construct(target, args) {{
                    args[0] = rewrite(args[0]);
                    const socket = Reflect.construct(target, args);
                    if (String(args[0]).endsWith('/ws')) window.statusSocket = socket;
                    return socket;
                }} }});
            """,
            )
            await page.command("Page.navigate", url=frontend)
            await page.wait("!!document.getElementById('access-key')")
            await page.evaluate("document.getElementById('access-key').focus()")
            await page.command("Input.insertText", text=TEST_ACCESS_KEY)
            await page.evaluate("document.querySelector('button[type=submit]').click()")
            await page.wait(
                "document.querySelector('[role=alert]')?.textContent.includes('認証の応答が不正です')"
            )
            assert (
                await page.evaluate("document.getElementById('access-key').value")
                == TEST_ACCESS_KEY
            )
            assert await page.evaluate("!document.querySelector('.history-main')")
            faults["login"] = False
            await page.evaluate("document.querySelector('button[type=submit]').click()")
            await page.wait(
                "document.querySelectorAll('.history-main').length === 2 && "
                "!document.querySelector('textarea').disabled"
            )
            await page.evaluate(
                "document.querySelector('.history-main[title=保存済みの売上報告]').click()"
            )
            await page.wait("document.body.textContent.includes('タスク: 保存済みの売上報告')")
            await page.evaluate("document.querySelector('textarea').focus()")
            await page.command("Input.insertText", text="編集中の指示を残す")
            for fault in ("null", "wrong-id"):
                faults["detail"] = fault
                await page.evaluate(
                    "document.querySelector('.history-main[title=別の保存済み報告]').click()"
                )
                await page.wait(
                    "document.querySelector('[role=alert]')?.textContent.includes('タスク詳細の応答が不正です')"
                )
                assert (
                    await page.evaluate(
                        "document.querySelector('[role=alert]').parentElement"
                        ".querySelector('.history-main').title"
                    )
                    == second.instruction
                )
                assert (
                    await page.evaluate(
                        "document.querySelector('.history-item.selected .history-main').title"
                    )
                    == first.instruction
                )
                assert (
                    await page.evaluate("document.querySelector('textarea').value")
                    == "編集中の指示を残す"
                )
            faults["detail"] = None
            await page.evaluate(
                "document.querySelector('.history-main[title=別の保存済み報告]').click()"
            )
            await page.wait(
                "document.body.textContent.includes('タスク: 別の保存済み報告') && "
                "!document.querySelector('[role=alert]')"
            )
            reads = await page.evaluate("window.payloadReads['/vms']")
            history_reads = await page.evaluate("window.payloadReads['/tasks']")
            faults["lists"] = True
            await page.evaluate("window.statusSocket.close()")
            await page.wait(f"window.payloadReads['/tasks'] > {history_reads}")
            await page.wait(f"window.payloadReads['/vms'] > {reads}")
            assert (
                await page.evaluate(
                    "document.querySelector('.history-item.selected .history-main').title"
                )
                == second.instruction
            )
            assert (
                await page.evaluate(
                    "document.querySelector('.vm-tab.selected .vm-tab-name').textContent"
                )
                == vm.name
            )
            assert (
                await page.evaluate("document.querySelector('textarea').value")
                == "編集中の指示を残す"
            )
            assert await page.evaluate(
                "document.body.textContent.includes('タスク: 別の保存済み報告')"
            )
    finally:
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            ALLOWED_ORIGINS[:] = origins
