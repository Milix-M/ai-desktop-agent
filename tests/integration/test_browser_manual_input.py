"""Type through the actual browser viewer into a real desktop during a handoff."""

import asyncio
import base64
import json
import os
import socket
import uuid
from types import SimpleNamespace

import docker
import httpx
import pytest
import uvicorn

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.browser_access import ALLOWED_ORIGINS
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_pool import VmInfo
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol
from tests.browser import BROWSER, browser
from tests.http import AUTH_HEADERS, TEST_ACCESS_KEY
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
    pytest.mark.skipif(BROWSER is None, reason="Chromium or Chrome is required"),
]


@pytest.fixture
def frontend():
    if url := os.environ.get("FRONTEND_SMOKE_URL"):
        yield url
        return
    client = docker.from_env(timeout=15)
    container = client.containers.run(
        os.environ.get("FRONTEND_SMOKE_IMAGE", "ai-desktop-agent-frontend-smoke"),
        detach=True,
        ports={"3000/tcp": ("127.0.0.1", None)},
        labels={"ai-desktop-agent.test": "browser-handoff-" + uuid.uuid4().hex},
    )
    try:
        container.reload()
        port = container.attrs["NetworkSettings"]["Ports"]["3000/tcp"][0]["HostPort"]
        yield f"http://127.0.0.1:{port}"
    finally:
        container.remove(force=True)
        client.close()


def input_state(desktop, env):
    """Read the guest X server's actual key and pointer state."""
    script = """
import ctypes as c
import json
x = c.CDLL('libX11.so.6')
x.XOpenDisplay.argtypes = [c.c_char_p]
x.XOpenDisplay.restype = c.c_void_p
x.XDefaultRootWindow.argtypes = [c.c_void_p]
x.XDefaultRootWindow.restype = c.c_ulong
x.XQueryKeymap.argtypes = [c.c_void_p, c.c_void_p]
x.XQueryPointer.argtypes = [c.c_void_p, c.c_ulong, c.c_void_p, c.c_void_p,
                           c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p]
x.XCloseDisplay.argtypes = [c.c_void_p]
display = x.XOpenDisplay(None)
assert display
keys = (c.c_ubyte * 32)()
root, child = c.c_ulong(), c.c_ulong()
rx, ry, wx, wy = c.c_int(), c.c_int(), c.c_int(), c.c_int()
mask = c.c_uint()
try:
    x.XQueryKeymap(display, keys)
    assert x.XQueryPointer(display, x.XDefaultRootWindow(display), c.byref(root),
                           c.byref(child), c.byref(rx), c.byref(ry), c.byref(wx),
                           c.byref(wy), c.byref(mask))
    print(json.dumps({'keys': [n for n in range(256) if keys[n // 8] & (1 << (n % 8))],
                      'buttons': mask.value & 0x1f00}))
finally:
    x.XCloseDisplay(display)
"""
    result = desktop.exec_run(["python3", "-c", script], user="agent", environment=env)
    assert result.exit_code == 0, result.output.decode()
    return json.loads(result.output)


@pytest.mark.parametrize(
    "status_channel", ["connected", "status-disconnected", "malformed", "stalled"]
)
@pytest.mark.parametrize(
    "held_input", [None, "key", "pointer"], ids=["typing", "held-key", "held-pointer"]
)
async def test_browser_handoff_accepts_input_only_while_the_model_is_waiting(
    desktop, frontend, tmp_path, monkeypatch, status_channel, held_input
):
    disconnect_state = status_channel in {"status-disconnected", "malformed"}
    external_resume = status_channel != "connected"
    desktop.reload()
    address = next(iter(desktop.attrs["NetworkSettings"]["Networks"].values()))["IPAddress"]
    vm = VmInfo(
        id="desktop",
        name="desktop",
        kind="container",
        status="running",
        health="healthy",
        vnc_host=address,
    )
    pool = SimpleNamespace(
        get_vm=lambda _: vm,
        default_vm=lambda: vm,
        prepare_vm=lambda _: None,
        isolate_environments=lambda: None,
        list_vms=lambda: [vm],
    )
    store = TaskStore(tmp_path)
    sessions = []
    agent_resumed = asyncio.Event()
    finish_agent = asyncio.Event()
    corrupt_next_state = asyncio.Event()
    stall_state = asyncio.Event()
    original_send = server._send_json

    async def send_status(ws, payload, timeout=5.0):
        # Keep the status socket and heartbeat healthy while the application
        # updates silently stop. The viewer must not retain input authority.
        if stall_state.is_set() and payload.get("type") != "pong":
            return True
        if corrupt_next_state.is_set() and payload.get("type") == "state":
            corrupt_next_state.clear()
            return await original_send(ws, None, timeout)
        return await original_send(ws, payload, timeout)

    if status_channel in {"malformed", "stalled"}:
        monkeypatch.setattr(server, "_send_json", send_status)

    class ManualProvider(MockLLMProvider):
        async def decide_next_action(self, *args, **kwargs):
            self._decide_calls.append(None)
            if len(self._decide_calls) > 1 and held_input is not None:
                # Observe the guest at the next model call, before the test can
                # release the browser's stale local key/button state itself.
                assert input_state(desktop, env) == {"keys": [], "buttons": 0}
                agent_resumed.set()
                await finish_agent.wait()
            kind = (
                ActionType.REQUEST_USER_INPUT
                if len(self._decide_calls) == 1
                else ActionType.SUBTASK_COMPLETE
            )
            return ActionDecision(
                Action(
                    kind,
                    {"reason": "画面へ確認用の文字を入力してください"}
                    if kind == ActionType.REQUEST_USER_INPUT
                    else {},
                ),
                "確認用の文字を保存する",
                1.0,
                "scripted handoff",
            )

    model = ManualProvider()

    def create(display):
        session = TaskSession(llm=model, display=display, store=store, decision=None)
        sessions.append(session)
        return session

    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_create_session", create)
    # The fixture replaces environment discovery and model decisions. HTTP auth,
    # task state, WebSocket synchronization, noVNC and desktop input remain real.
    original_origins = ALLOWED_ORIGINS[:]
    ALLOWED_ORIGINS.append(frontend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api_port = listener.getsockname()[1]
    api = uvicorn.Server(
        uvicorn.Config(server.app, lifespan="off", log_level="warning", ws=AgentWebSocketProtocol)
    )
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    proof = "/tmp/browser-handoff-" + uuid.uuid4().hex
    content = "typed-in-the-browser"
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
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
        desktop.exec_run(
            ["xfce4-terminal", "--disable-server", "--title=Browser handoff"],
            detach=True,
            user="agent",
            environment=env,
        )
        async with asyncio.timeout(10):
            while True:
                window = desktop.exec_run(
                    ["xdotool", "search", "--onlyvisible", "--name", "Browser handoff"],
                    user="agent",
                    environment=env,
                )
                if window.exit_code == 0:
                    desktop.exec_run(
                        ["xdotool", "windowactivate", window.output.decode().splitlines()[0]],
                        user="agent",
                        environment=env,
                    )
                    break
                await asyncio.sleep(0.1)
        async with browser(tmp_path) as page:
            # Keep the application's fixed public port away from an existing local
            # installation. Both REST and WS are routed to this test's real API.
            await page.command(
                "Page.addScriptToEvaluateOnNewDocument",
                source=f"""
                const rewrite = value => String(value).replace(':8081/', ':{api_port}/');
                const fetchOriginal = window.fetch.bind(window);
                window.fetch = (url, init) => fetchOriginal(rewrite(url), init);
                const Socket = window.WebSocket;
                window.blockStatus = false;
                window.WebSocket = new Proxy(Socket, {{
                    construct(target, args) {{
                        args[0] = rewrite(args[0]);
                        const status = args[0].endsWith('/ws');
                        if (status && window.blockStatus) args[0] += '-offline';
                        const socket = Reflect.construct(target, args);
                        if (status) window.statusSocket = socket;
                        if (args[0].includes('/vnc/')) window.vncSocket = socket;
                        return socket;
                    }}
                }});
            """,
            )
            await page.command("Page.navigate", url=frontend)
            await page.wait("!!document.getElementById('access-key')")
            await page.evaluate("document.getElementById('access-key').focus()")
            await page.command("Input.insertText", text=TEST_ACCESS_KEY)
            await page.evaluate("document.querySelector('button[type=submit]').click()")
            await page.wait(
                "!!document.querySelector('.vnc-screen canvas') && "
                "!document.getElementById('instruction').disabled"
            )
            await page.wait("document.body.textContent.includes('VNC 接続中')")
            await page.evaluate("document.querySelector('.vnc-screen canvas').focus()")
            await page.type_keys(f"printf {content} > {proof}\n")
            await asyncio.sleep(0.2)  # Allow any mistakenly forwarded input to reach the guest.
            assert desktop.exec_run(["test", "-e", proof]).exit_code != 0
            await page.evaluate("document.getElementById('instruction').focus()")
            await page.command("Input.insertText", text="確認用の文字は手動で入力します")
            await page.evaluate("document.getElementById('submit-btn').click()")
            await page.wait("document.body.textContent.includes('画面を送信して再開')")
            assert len(model._decide_calls) == 1
            await page.evaluate("document.querySelector('.vnc-screen canvas').focus()")
            await page.type_keys(f"printf {content} > {proof}\nclear\n")
            async with asyncio.timeout(10):
                while desktop.exec_run(["cat", proof]).output != content.encode():
                    await asyncio.sleep(0.1)
            assert len(model._decide_calls) == 1
            if held_input == "key":
                await page.command(
                    "Input.dispatchKeyEvent", type="keyDown", key="Control", code="ControlLeft"
                )
                async with asyncio.timeout(5):
                    while not input_state(desktop, env)["keys"]:
                        await asyncio.sleep(0.05)
            elif held_input == "pointer":
                center = await page.evaluate(
                    "(() => { const r = document.querySelector('.vnc-screen canvas')"
                    ".getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; })()"
                )
                await page.command(
                    "Input.dispatchMouseEvent",
                    type="mousePressed",
                    button="left",
                    buttons=1,
                    clickCount=1,
                    **center,
                )
                async with asyncio.timeout(5):
                    while not input_state(desktop, env)["buttons"]:
                        await asyncio.sleep(0.05)
            if external_resume:
                if status_channel == "stalled":
                    stall_state.set()
                else:
                    await page.evaluate(
                        "window.blockStatus = true; "
                        "window.faultedStatusSocket = window.statusSocket"
                    )
                    if status_channel == "malformed":
                        corrupt_next_state.set()
                        await sessions[0]._emit_state_change()
                    else:
                        await page.evaluate("window.statusSocket.close()")
                    await page.wait("window.faultedStatusSocket.readyState === WebSocket.CLOSED")
                assert await page.evaluate("window.vncSocket.readyState === WebSocket.OPEN")
                async with httpx.AsyncClient(trust_env=False, headers=AUTH_HEADERS) as client:
                    response = await client.post(
                        f"http://127.0.0.1:{api_port}/tasks/current/resume",
                        params={"vm_id": vm.id, "session_id": sessions[0].id},
                        json={
                            "manual_input_id": sessions[0].manual_input["id"],
                            "allow_screen_share": True,
                        },
                    )
                    assert response.status_code == 200, response.text
                if held_input is None:
                    await asyncio.wait_for(asyncio.shield(sessions[0]._task), 10)
            else:
                await page.evaluate(
                    "[...document.querySelectorAll('button')]"
                    ".find(b => b.textContent === '画面を送信して再開').click()"
                )
                if held_input is None:
                    await page.wait("document.body.textContent.includes('COMPLETED')")
            if held_input is not None:
                await asyncio.wait_for(agent_resumed.wait(), 5)
                try:
                    async with asyncio.timeout(5):
                        while (state := input_state(desktop, env)) != {"keys": [], "buttons": 0}:
                            await asyncio.sleep(0.05)
                except TimeoutError:
                    pytest.fail(f"Browser handoff left guest input pressed: {state}")
                assert state == {"keys": [], "buttons": 0}
                if held_input == "key":
                    await page.command(
                        "Input.dispatchKeyEvent", type="keyUp", key="Control", code="ControlLeft"
                    )
                else:
                    if status_channel != "stalled":
                        assert await page.evaluate(
                            "document.getElementById('noVNC_mouse_capture_elem')"
                            ".style.display === 'none'"
                        )
                    await page.command(
                        "Input.dispatchMouseEvent",
                        type="mouseReleased",
                        button="left",
                        buttons=0,
                        clickCount=1,
                        **center,
                    )
                finish_agent.set()
                if external_resume:
                    await asyncio.wait_for(asyncio.shield(sessions[0]._task), 10)
                else:
                    await page.wait("document.body.textContent.includes('COMPLETED')")
            await page.evaluate("document.querySelector('.vnc-screen canvas').focus()")
            await page.type_keys(f"printf blocked >> {proof}\n")
            await asyncio.sleep(0.2)
            assert desktop.exec_run(["cat", proof]).output == content.encode()
            if status_channel == "stalled":
                assert await page.evaluate("window.statusSocket.readyState === WebSocket.OPEN")
                assert await page.evaluate("window.vncSocket.readyState === WebSocket.OPEN")
                assert await page.evaluate(
                    "document.body.textContent.includes('画面を送信して再開')"
                )
            if disconnect_state:
                assert await page.evaluate("window.vncSocket.readyState === WebSocket.OPEN")
                await page.wait("document.body.textContent.includes('バックエンド 未接続')")
                await page.evaluate(
                    "document.querySelector('.conn-list').scrollIntoView({block:'center'})"
                )
                screenshot = await page.command("Page.captureScreenshot", format="png")
                (tmp_path / "status-disconnected.png").write_bytes(
                    base64.b64decode(screenshot["data"])
                )
                await page.evaluate("window.blockStatus = false")
                await page.wait("document.body.textContent.includes('COMPLETED')")
            assert content not in json.dumps(store.load(sessions[0].id).to_dict())
    finally:
        for session in sessions:
            session.stop()
        await asyncio.gather(*(s._task for s in sessions if s._task), return_exceptions=True)
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            ALLOWED_ORIGINS[:] = original_origins
