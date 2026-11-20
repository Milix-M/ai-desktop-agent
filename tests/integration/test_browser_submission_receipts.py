"""Invalid browser receipts must not turn a repeated instruction into an old task."""

import asyncio
import base64
import json
import os
import socket

import httpx
import pytest
import uvicorn

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.browser_access import ALLOWED_ORIGINS
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_pool import VmInfo
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol
from tests.browser import BROWSER, browser
from tests.http import TEST_ACCESS_KEY
from tests.integration.test_browser_manual_input import frontend as frontend
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real browser opt-in"),
    pytest.mark.skipif(BROWSER is None, reason="Chromium or Chrome is required"),
]


async def test_a_nonstring_saved_key_cannot_replay_a_previous_completed_instruction(
    frontend, desktop, tmp_path, monkeypatch
):
    desktop.reload()
    address = next(iter(desktop.attrs["NetworkSettings"]["Networks"].values()))["IPAddress"]
    vm = VmInfo(
        id="desktop",
        name="作業環境",
        kind="container",
        status="running",
        health="healthy",
        vnc_host=address,
        managed=False,
    )

    class Pool:
        def isolate_environments(self):
            pass

        def list_vms(self):
            return [vm]

        def get_vm(self, vm_id):
            return vm if vm_id == vm.id else None

        def default_vm(self):
            return vm

        def prepare_vm(self, vm_id):
            assert vm_id == vm.id

    store = TaskStore(tmp_path / "data")
    sessions = []

    def create_session(display):
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)
        sessions.append(session)
        return session

    monkeypatch.setattr(server, "_pool", Pool())
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_create_session", create_session)
    origins = ALLOWED_ORIGINS[:]
    ALLOWED_ORIGINS.append(frontend)
    posts = []

    async def observe_requests(scope, receive, send):
        submission = (
            scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/tasks"
        )
        observed = None
        if submission:
            observed = {"key": dict(scope["headers"])[b"idempotency-key"].decode()}
            posts.append(observed)

        async def observe_response(message):
            if observed is not None and message["type"] == "http.response.body":
                observed["response"] = json.loads(message["body"])
            await send(message)

        await server.app(scope, receive, observe_response)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(
        uvicorn.Config(
            observe_requests,
            lifespan="off",
            log_level="warning",
            ws=AgentWebSocketProtocol,
        )
    )
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    instruction = "画面を確認する"
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
                    args[0] = rewrite(args[0]); return Reflect.construct(target, args);
                }} }});
            """,
            )
            await page.command("Page.navigate", url=frontend)
            await page.wait("!!document.getElementById('access-key')")
            await page.evaluate("document.getElementById('access-key').focus()")
            await page.command("Input.insertText", text=TEST_ACCESS_KEY)
            await page.evaluate("document.querySelector('button[type=submit]').click()")
            await page.wait("document.getElementById('instruction')?.disabled === false")
            await page.wait("!!document.querySelector('.vnc-screen canvas')")
            for execution in range(2):
                await page.evaluate("document.getElementById('instruction').focus()")
                await page.command("Input.insertText", text=instruction)
                await page.evaluate("document.getElementById('submit-btn').click()")
                await page.wait("document.getElementById('instruction')?.value === ''")
                await page.wait("document.getElementById('submit-btn')?.disabled === true")
                async with asyncio.timeout(20):
                    while len(posts) <= execution or "response" not in posts[execution]:
                        await asyncio.sleep(0.05)
                    while any(session.is_running for session in sessions):
                        await asyncio.sleep(0.05)
                await page.wait("document.getElementById('instruction')?.disabled === false")
                if execution == 0:
                    assert len(sessions) == 1
                    assert store.load(sessions[0].id).success is True
                    corrupted = await page.evaluate(f"""(async () => {{
                        const payload = JSON.stringify([
                            {json.dumps(instruction)}, 'desktop', false
                        ]);
                        const digest = await crypto.subtle.digest(
                            'SHA-256', new TextEncoder().encode(payload)
                        );
                        const fingerprint = Array.from(new Uint8Array(digest), byte =>
                            byte.toString(16).padStart(2, '0')).join('');
                        const receipt = [{{fingerprint,
                            requestId:[{json.dumps(posts[0]["key"])}]}}];
                        sessionStorage.setItem('ai-desktop-agent-task-submissions',
                            JSON.stringify(receipt));
                        return receipt;
                    }})()""")
                    (tmp_path / "malformed-receipt.json").write_text(json.dumps(corrupted))
            (tmp_path / "submission-results.json").write_text(json.dumps(posts, indent=2))
            screenshot = await page.command("Page.captureScreenshot", format="png")
            (tmp_path / "submission-results.png").write_bytes(base64.b64decode(screenshot["data"]))
            assert len(posts) == 2
            assert posts[0]["key"] != posts[1]["key"], posts
            assert posts[0]["response"]["session_id"] != posts[1]["response"]["session_id"]
            assert len(sessions) == 2 and len(store.list(limit=None)) == 2
            assert all(record.success is True for record in store.list(limit=None))
            assert (
                await page.evaluate("sessionStorage.getItem('ai-desktop-agent-task-submissions')")
                == "[]"
            )
    finally:
        for session in sessions:
            session.stop()
        await asyncio.gather(*(session.aclose() for session in sessions), return_exceptions=True)
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            ALLOWED_ORIGINS[:] = origins
