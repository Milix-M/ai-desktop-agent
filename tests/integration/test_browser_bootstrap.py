"""Recover actual initial script-load failures without replaying application work."""

import asyncio
import json
import os
import socket

import httpx
import pytest
import uvicorn

from tests.browser import BROWSER, browser
from tests.integration.test_browser_manual_input import frontend as frontend

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real browser opt-in"),
    pytest.mark.skipif(BROWSER is None, reason="Chromium or Chrome is required"),
]


@pytest.mark.parametrize("failure", ["temporary", "persistent", "storage-blocked"])
async def test_initial_script_failure_has_bounded_recovery_and_a_manual_retry(
    frontend, tmp_path, failure
):
    requests = []

    async def auth(scope, receive, send):
        requests.append((scope["method"], scope["path"]))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"access-control-allow-origin", frontend.encode()),
                    (b"access-control-allow-credentials", b"true"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b'{"authenticated":false}'})

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(auth, lifespan="off", log_level="warning"))
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
                window.name = String(Number(window.name || 0) + 1);
                if ({json.dumps(failure == "storage-blocked")}) {{
                    Object.defineProperty(window, 'sessionStorage', {{
                        get() {{ throw new DOMException('blocked', 'SecurityError'); }}
                    }});
                }}
                const original = window.fetch.bind(window);
                window.fetch = (url, init) => original(
                    String(url).replace(':8081/', ':{port}/'), init);
                """,
            )
            await page.command("Network.setBlockedURLs", urls=["*/_next/static/*.js"])
            await page.command("Page.navigate", url=frontend)
            if failure != "storage-blocked":
                await page.wait(
                    "sessionStorage.getItem('ai-desktop-agent-bootstrap-retry') === '1'"
                )
            assert requests == []
            if failure == "temporary":
                await page.command("Network.setBlockedURLs", urls=[])
            else:
                await page.wait(
                    "document.querySelector('[role=alert]')?.textContent"
                    ".includes('画面を読み込めませんでした')"
                )
                initial_loads = "2" if failure == "persistent" else "1"
                assert await page.evaluate("window.name") == initial_loads
                # Observe longer than the single automatic retry delay: no reload loop.
                await asyncio.sleep(1.2)
                assert await page.evaluate("window.name") == initial_loads
                assert requests == []
                await page.command("Network.setBlockedURLs", urls=[])
                await page.evaluate(
                    "[...document.querySelectorAll('button')]"
                    ".find(button => button.textContent === '再読み込み').click()"
                )
            await page.wait("!!document.getElementById('access-key')")
            expected_loads = "3" if failure == "persistent" else "2"
            assert await page.evaluate("window.name") == expected_loads
            if failure != "storage-blocked":
                assert (
                    await page.evaluate(
                        "sessionStorage.getItem('ai-desktop-agent-bootstrap-retry')"
                    )
                    is None
                )
            assert requests == [("GET", "/auth/status")]
            # A later chunk error must preserve the operator's unsent login input.
            await page.evaluate("document.getElementById('access-key').focus()")
            await page.command("Input.insertText", text="unsent-operator-key")
            await page.evaluate(
                "(() => { const script = document.createElement('script');"
                "script.src = '/_next/static/missing-late-chunk.js';"
                "document.head.append(script); })()"
            )
            await page.wait(
                "performance.getEntriesByType('resource').some(entry => "
                "entry.name.endsWith('/_next/static/missing-late-chunk.js'))"
            )
            await asyncio.sleep(1.2)
            assert (
                await page.evaluate("document.getElementById('access-key').value")
                == "unsent-operator-key"
            )
            assert await page.evaluate("window.name") == expected_loads
    finally:
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
