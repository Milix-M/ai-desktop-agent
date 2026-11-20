"""Real container, browser, and VNC checks. Opt in with RUN_DESKTOP_SMOKE=1."""

import asyncio
import contextlib
import errno
import os
import shlex
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import docker
import pytest
from httpx import ASGITransport

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, DecompositionResult, VerificationResult
from ai_desktop_agent.agent.state import AgentState, Subtask
from ai_desktop_agent.server import app as server_app
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.http import AuthenticatedClient

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.fixture(scope="session")
def vnc_runtime():
    yield
    VNCClient.shutdown_runtime()


@pytest.fixture(scope="module")
def desktop(vnc_runtime):
    client = docker.from_env(timeout=10)
    image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    owner = uuid.uuid4().hex
    container = None
    try:
        container = client.containers.run(
            image,
            detach=True,
            ports={"5900/tcp": ("127.0.0.1", None)},
            shm_size="1g",
            labels={"ai-desktop-agent.smoke": owner},
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            container.reload()
            health = container.attrs.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                yield container
                return
            if container.status == "exited":
                pytest.fail(container.logs().decode())
            time.sleep(0.5)
        pytest.fail("VNC did not become healthy: " + container.logs().decode())
    finally:
        try:
            # Docker may accept creation before its HTTP response is lost.
            owned = (
                [container]
                if container is not None
                else client.containers.list(
                    all=True, filters={"label": "ai-desktop-agent.smoke=" + owner}
                )
            )
            for item in owned:
                with contextlib.suppress(docker.errors.NotFound):
                    item.remove(force=True)
        finally:
            client.close()


def test_firefox_can_render_a_page_without_snap(desktop):
    desktop.exec_run(["mkdir", "-p", "/tmp/smoke-firefox-profile"], user="agent")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "agent",
            desktop.id,
            "firefox",
            "--headless",
            "--no-remote",
            "--profile",
            "/tmp/smoke-firefox-profile",
            "--screenshot",
            "/tmp/firefox-smoke.png",
            "data:text/html,<h1>Desktop smoke</h1>",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert desktop.exec_run(["test", "-s", "/tmp/firefox-smoke.png"]).exit_code == 0


async def test_vnc_capture_accepts_an_ipv6_literal_without_prebracketing(desktop):
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    handlers = set()

    async def bridge(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        upstream = None
        pumps = []
        try:
            remote_reader, upstream = await asyncio.open_connection("127.0.0.1", port)

            async def copy(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()

            pumps = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(remote_reader, writer)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (writer, upstream):
                if connection is not None:
                    connection.close()
                    with contextlib.suppress(ConnectionResetError):
                        await connection.wait_closed()
            handlers.discard(task)

    try:
        server = await asyncio.start_server(bridge, "::1", 0, family=socket.AF_INET6)
    except OSError as error:
        if error.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
            pytest.skip("IPv6 loopback is not available")
        raise
    display = VNCClient()
    try:
        await asyncio.to_thread(display.connect, "::1", server.sockets[0].getsockname()[1])
        screenshot = await asyncio.to_thread(display.capture_screen)
        assert screenshot.width > 0 and screenshot.height > 0
    finally:
        await asyncio.to_thread(display.disconnect)
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), 5)


@pytest.mark.parametrize("phase", ["connect", "session"])
async def test_cancelled_task_submission_closes_real_vnc(desktop, tmp_path, monkeypatch, phase):
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    entered, release = threading.Event(), threading.Event()
    display = VNCClient()
    provider = MockLLMProvider()
    provider.aclose = AsyncMock()
    connections = []

    def connect(_):
        display.connect("127.0.0.1", port)
        connections.append(display._client)
        if phase == "connect":
            entered.set()
            assert release.wait(5)
        return display

    def create(display):
        session = TaskSession(llm=provider, display=display, decision=None)
        if phase == "session":
            entered.set()
            assert release.wait(5)
        return session

    monkeypatch.setattr(server_app, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server_app, "_sessions", {})
    monkeypatch.setattr(server_app, "_active_session", None)
    monkeypatch.setattr(server_app, "_vm_operations", server_app.AsyncKeyedLocks())
    monkeypatch.setattr(server_app, "_android_provisioner", None)
    monkeypatch.setattr(server_app, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server_app, "_default_vm_id", lambda: "desktop")
    monkeypatch.setattr(server_app, "_connect_vm_display", connect)
    monkeypatch.setattr(server_app, "_create_session", create)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server_app.app), base_url="http://test"
    ) as api:
        request = asyncio.create_task(api.post("/tasks", json={"instruction": "画面を確認する"}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            transport = connections[0].protocol.transport
            assert not transport.disconnected
            request.cancel()
            await asyncio.sleep(0)
            assert not request.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert connections[0].closed
            assert not display.is_connected
            async with asyncio.timeout(2):
                while not transport.disconnected:
                    await asyncio.sleep(0.01)
            assert not server_app._sessions
            assert server_app._active_session is None
            if phase == "session":
                provider.aclose.assert_awaited_once()
            else:
                provider.aclose.assert_not_called()
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await asyncio.to_thread(display.disconnect)


async def test_replayed_submission_appends_once_via_real_vnc(desktop, tmp_path, monkeypatch):
    title = "Retry smoke " + uuid.uuid4().hex
    proof = "/tmp/retry-proof-" + uuid.uuid4().hex
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    desktop.exec_run(
        [
            "xfce4-terminal",
            "--disable-server",
            f"--title={title}",
            "--command=bash --noprofile --norc",
        ],
        user="agent",
        environment=env,
        detach=True,
    )
    window = None
    for _ in range(60):
        found = desktop.exec_run(
            ["xdotool", "search", "--onlyvisible", "--name", title], user="agent", environment=env
        )
        if found.exit_code == 0:
            window = found.output.decode().splitlines()[0]
            desktop.exec_run(["xdotool", "windowactivate", window], user="agent", environment=env)
            break
        await asyncio.sleep(0.2)
    assert window is not None
    await asyncio.sleep(0.3)
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    store = TaskStore(tmp_path)
    made = []

    def connect(_):
        display = VNCClient()
        display.connect("127.0.0.1", port)
        return display

    def create(display):
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)

        async def finish():
            await session._execute_recorded_action(
                Action(ActionType.TYPE, {"text": f"printf 'once\\n' >> {shlex.quote(proof)}\n"}),
                "1行追記する",
            )
            session.loop.context.state = AgentState.COMPLETED

        session._execute_phase = finish
        made.append(session)
        return session

    monkeypatch.setattr(server_app, "_store", store)
    monkeypatch.setattr(server_app, "_sessions", {})
    monkeypatch.setattr(server_app, "_active_session", None)
    monkeypatch.setattr(server_app, "_vm_operations", server_app.AsyncKeyedLocks())
    monkeypatch.setattr(server_app, "_android_provisioner", None)
    monkeypatch.setattr(server_app, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server_app, "_default_vm_id", lambda: "desktop")
    monkeypatch.setattr(server_app, "_connect_vm_display", connect)
    monkeypatch.setattr(server_app, "_create_session", create)
    headers = {"Idempotency-Key": "real-vnc-retry-" + uuid.uuid4().hex}
    body = {"instruction": "1行だけ追記する", "vm_id": "desktop"}
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server_app.app), base_url="http://test"
        ) as api:
            first = await api.post("/tasks", json=body, headers=headers)
            assert first.status_code == 200
            await made[0]._task
            replay = await api.post("/tasks", json=body, headers=headers)
            assert replay.status_code == 200 and replay.json()["replayed"] is True
            assert replay.json()["session_id"] == first.json()["session_id"]
            assert len(made) == 1
            for _ in range(30):
                actual = desktop.exec_run(["cat", proof], user="agent")
                if actual.exit_code == 0:
                    break
                await asyncio.sleep(0.1)
            assert actual.exit_code == 0 and actual.output == b"once\n"
            await api.delete("/tasks/" + first.json()["session_id"])
            assert (await api.post("/tasks", json=body, headers=headers)).status_code == 410
            assert desktop.exec_run(["cat", proof], user="agent").output == b"once\n"
    finally:
        for session in made:
            session.stop()
        await asyncio.gather(*(s._task for s in made if s._task), return_exceptions=True)
        desktop.exec_run(["xdotool", "windowclose", window], user="agent", environment=env)


async def test_stale_controls_do_not_touch_a_replacement_with_real_vnc(
    desktop, tmp_path, monkeypatch
):
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    store = TaskStore(tmp_path)
    made = []

    def connect(_):
        display = VNCClient()
        display.connect("127.0.0.1", port)
        return display

    def create(display):
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)
        session.ready = asyncio.Event()

        async def wait():
            screenshot = await session._capture_screenshot()
            assert screenshot.width > 0 and screenshot.height > 0
            session.ready.set()
            await asyncio.Event().wait()

        session._execute_phase = wait
        made.append(session)
        return session

    monkeypatch.setattr(server_app, "_store", store)
    monkeypatch.setattr(server_app, "_sessions", {})
    monkeypatch.setattr(server_app, "_active_session", None)
    monkeypatch.setattr(server_app, "_vm_operations", server_app.AsyncKeyedLocks())
    monkeypatch.setattr(server_app, "_android_provisioner", None)
    monkeypatch.setattr(server_app, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server_app, "_default_vm_id", lambda: "desktop")
    monkeypatch.setattr(server_app, "_connect_vm_display", connect)
    monkeypatch.setattr(server_app, "_create_session", create)
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server_app.app), base_url="http://test"
        ) as api:
            for instruction in ["先行タスク", "後発タスク"]:
                response = await api.post(
                    "/tasks", json={"instruction": instruction, "vm_id": "desktop"}
                )
                assert response.status_code == 200
                await asyncio.wait_for(made[-1].ready.wait(), 5)
            previous, replacement = made
            assert previous._task.done() and not previous.display.is_connected
            target = {"vm_id": "desktop", "session_id": replacement.id}
            for action, state in [("pause", AgentState.PAUSED), ("resume", AgentState.EXECUTING)]:
                denied = await api.post(
                    f"/tasks/current/{action}",
                    params={"vm_id": "desktop", "session_id": previous.id},
                )
                assert denied.status_code == 409
                assert (
                    await api.post(f"/tasks/current/{action}", params=target)
                ).status_code == 200
                assert replacement.loop.state == state
                assert store.load(replacement.id).state == state.value
            denied = await api.post(
                "/tasks/current/stop", params={"vm_id": "desktop", "session_id": previous.id}
            )
            assert denied.status_code == 409 and replacement.is_running
            assert replacement.display.is_connected
            assert (await api.post("/tasks/current/stop", params=target)).status_code == 200
            await asyncio.wait_for(asyncio.gather(replacement._task, return_exceptions=True), 5)
            assert not replacement.display.is_connected
            assert store.load(replacement.id).state == "failed"
    finally:
        for session in made:
            session.stop()
        await asyncio.gather(*(s._task for s in made if s._task), return_exceptions=True)


@pytest.mark.parametrize("content", ["desktop-smoke-ok", "日本語の文章を保存する 👩‍💻 café"])
async def test_scripted_session_writes_a_file_via_real_vnc(desktop, tmp_path, content):
    output_path = f"/tmp/desktop-smoke-{uuid.uuid4().hex}"
    title = f"Desktop smoke {uuid.uuid4().hex}"
    desktop.exec_run(
        [
            "xfce4-terminal",
            "--disable-server",
            f"--title={title}",
            "--command=bash --noprofile --norc",
        ],
        user="agent",
        environment={"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"},
        detach=True,
    )
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    for _ in range(60):
        found = desktop.exec_run(
            ["xdotool", "search", "--onlyvisible", "--name", title],
            user="agent",
            environment=env,
        )
        if found.exit_code == 0:
            window = found.output.decode().splitlines()[0]
            desktop.exec_run(["xdotool", "windowactivate", window], user="agent", environment=env)
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("Terminal window did not appear")
    await asyncio.sleep(0.5)

    class ScriptedProvider(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.actions = iter(
                [
                    Action(
                        ActionType.TYPE,
                        {"text": f"printf %s {shlex.quote(content)} > {output_path}"},
                    ),
                    Action(ActionType.KEY_PRESS, {"key": "enter"}),
                    Action(ActionType.SUBTASK_COMPLETE),
                ]
            )

        async def decompose_task(self, *args, **kwargs):
            return DecompositionResult([Subtask("write", "確認用のファイルを保存する")], "scripted")

        async def decide_next_action(self, *args, **kwargs):
            return ActionDecision(next(self.actions), "ファイルを保存する", 1.0, "scripted")

        async def verify_result(self, *args, **kwargs):
            result = desktop.exec_run(["cat", output_path])
            return VerificationResult(
                result.exit_code == 0 and result.output == content.encode("utf-8"),
                f"file check: {result.exit_code} {result.output[:300]!r}",
                "actual file contents",
            )

    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    display = VNCClient()
    await asyncio.to_thread(display.connect, "127.0.0.1", port)
    screenshot = await asyncio.to_thread(display.capture_raw)
    assert (screenshot.width, screenshot.height) == (1280, 800)
    store = TaskStore(tmp_path)
    session = TaskSession(llm=ScriptedProvider(), display=display, store=store, decision=None)
    assert await session.run("確認用のファイルを保存する")
    assert desktop.exec_run(["cat", output_path]).output == content.encode("utf-8")
    record = store.load(session.id)
    assert record.state == "completed" and record.success
    assert len(record.actions) == 3
    assert all(action.duration_ms > 0 for action in record.actions)


async def test_unicode_paragraphs_can_be_typed_and_saved_in_an_editor(desktop):
    output = f"/tmp/入力確認-{uuid.uuid4().hex}.txt"
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    assert desktop.exec_run(["touch", output], user="agent").exit_code == 0
    desktop.exec_run(
        ["mousepad", "--disable-server", output],
        user="agent",
        environment=env,
        detach=True,
    )
    for _ in range(60):
        found = desktop.exec_run(
            ["xdotool", "search", "--onlyvisible", "--class", "Mousepad"],
            user="agent",
            environment=env,
        )
        if found.exit_code == 0:
            desktop.exec_run(
                ["xdotool", "windowactivate", found.output.decode().splitlines()[0]],
                user="agent",
                environment=env,
            )
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail("Editor did not open")
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    display = VNCClient()
    await asyncio.to_thread(display.connect, "127.0.0.1", port)
    content = '日本語の長文とリンクではない文字\n\t👩‍💻 café e\u0301 "$()<>\\"\n'
    content += "".join(chr(0x4E00 + n) for n in range(2200))
    addition = "\n続けて入力した文章 🦊"
    try:
        for paragraph in (content, addition):
            await asyncio.to_thread(display.type_text, paragraph)
            await asyncio.sleep(0.3)
        await asyncio.to_thread(display.key_combo, ["ctrl", "s"])
        for _ in range(50):
            result = desktop.exec_run(["cat", output], user="agent")
            if result.output == (content + addition).encode("utf-8"):
                break
            await asyncio.sleep(0.1)
        assert result.exit_code == 0 and result.output == (content + addition).encode("utf-8")
    finally:
        await asyncio.to_thread(display.disconnect)


async def test_manual_password_input_is_not_sent_to_the_model_or_task_history(
    desktop, tmp_path, monkeypatch, caplog
):
    import json
    import logging

    from httpx import ASGITransport

    from ai_desktop_agent.server import app as server_app
    from tests.http import AuthenticatedClient

    proof = f"/tmp/manual-password-{uuid.uuid4().hex}"
    code = """from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs
import sys
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path == '/ready':
   Path(sys.argv[1]+'.ready').touch()
   self.send_response(204)
   self.end_headers()
   return
  self.send_response(200)
  self.send_header('Content-Type','text/html; charset=utf-8')
  self.end_headers()
  self.wfile.write(b'<title>Manual input login</title><form method="post">'
   b'<label>Password <input type="password" name="password" autofocus></label>'
   b'<button>Login</button></form>'
   b'<script>requestAnimationFrame(()=>fetch("/ready"))</script>')
 def do_POST(self):
  value=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())['password'][0]
  Path(sys.argv[1]).write_text(value)
  self.send_response(200)
  self.send_header('Content-Type','text/html; charset=utf-8')
  self.end_headers()
  self.wfile.write(b'<title>Manual input complete</title><h1>LOGIN COMPLETE</h1>')
 def log_message(self,*args):pass
HTTPServer(('127.0.0.1',8765),Handler).serve_forever()
"""
    desktop.exec_run(["python3", "-u", "-c", code, proof], user="agent", detach=True)
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    profile = "/tmp/manual-login-profile"
    desktop.exec_run(["mkdir", "-p", profile], user="agent")
    desktop.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; Path('/tmp/manual-login-profile/user.js').write_text("
            + repr(
                'user_pref("browser.shell.checkDefaultBrowser",false);\n'
                'user_pref("browser.aboutwelcome.enabled",false);\n'
                # The same test-only preference used by Mozilla's Remote Agent.
                'user_pref("termsofuse.bypassNotification",true);\n'
                'user_pref("signon.rememberSignons",false);'
            )
            + ")",
        ],
        user="agent",
    )
    desktop.exec_run(
        ["firefox", "--no-remote", "--profile", profile, "--new-window", "http://127.0.0.1:8765"],
        user="agent",
        environment=env,
        detach=True,
    )
    for _ in range(100):
        found = desktop.exec_run(
            ["xdotool", "search", "--onlyvisible", "--name", "Manual input login"],
            user="agent",
            environment=env,
        )
        ready = desktop.exec_run(["test", "-f", proof + ".ready"], user="agent")
        if found.exit_code == 0 and ready.exit_code == 0:
            desktop.exec_run(
                ["xdotool", "windowactivate", found.output.decode().splitlines()[0]],
                user="agent",
                environment=env,
            )
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail("Login form did not open")

    class ManualProvider(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.decisions = 0
            self.verifications = 0

        async def decompose_task(self, *args, **kwargs):
            return DecompositionResult([Subtask("login", "ログインする")], "手動入力を待つ")

        async def decide_next_action(self, *args, **kwargs):
            self.decisions += 1
            action = (
                Action(
                    ActionType.REQUEST_USER_INPUT,
                    {"reason": "パスワードを入力してログインしてください"},
                )
                if self.decisions == 1
                else Action(ActionType.SUBTASK_COMPLETE)
            )
            return ActionDecision(action, "ログインする", 1.0, "利用者による入力")

        async def verify_result(self, *args, **kwargs):
            self.verifications += 1
            complete = desktop.exec_run(
                ["xdotool", "search", "--name", "Manual input complete"],
                user="agent",
                environment=env,
            )
            return VerificationResult(complete.exit_code == 0, "ログイン完了画面", "完了を確認")

    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    display, user_display = VNCClient(), VNCClient()
    await asyncio.to_thread(display.connect, "127.0.0.1", port)
    model = ManualProvider()
    session = TaskSession(llm=model, display=display, store=TaskStore(tmp_path), decision=None)
    session.vm_id = "desktop"
    monkeypatch.setattr(server_app, "_sessions", {session.id: session})
    monkeypatch.setattr(server_app, "_active_session", session)
    pending = asyncio.Event()
    session.on_state_change(
        lambda state, _: pending.set() if state.value == "awaiting_user" else None
    )
    secret = "manual-private-" + uuid.uuid4().hex
    with caplog.at_level(logging.DEBUG):
        await session.start_async("ログイン情報は利用者が入力する")
        try:
            await asyncio.wait_for(pending.wait(), 10)
            calls = model.decisions, model.verifications
            await asyncio.to_thread(user_display.connect, "127.0.0.1", port)
            await asyncio.to_thread(user_display.type_text, secret)
            await asyncio.to_thread(user_display.key_press, "enter")
            for _ in range(50):
                received = desktop.exec_run(["cat", proof], user="agent")
                if received.exit_code == 0 and received.output == secret.encode():
                    break
                await asyncio.sleep(0.1)
            if received.exit_code != 0 or received.output != secret.encode():
                failure = await asyncio.to_thread(user_display.capture_raw)
                Path("/tmp/ai-desktop-agent-review-manual-input-failure.png").write_bytes(
                    failure.image_bytes
                )
            assert received.exit_code == 0 and received.output == secret.encode()
            assert (model.decisions, model.verifications) == calls
            async with AuthenticatedClient(
                transport=ASGITransport(app=server_app.app), base_url="http://test"
            ) as client:
                status = (await client.get("/tasks/current?vm_id=desktop")).json()
                denied = await client.post("/tasks/current/resume?vm_id=desktop")
                assert denied.status_code == 409
                resumed = await client.post(
                    "/tasks/current/resume?vm_id=desktop",
                    json={
                        "manual_input_id": status["manual_input"]["id"],
                        "allow_screen_share": True,
                    },
                )
                assert resumed.status_code == 200
            assert await asyncio.wait_for(session._task, 10)
            assert secret not in json.dumps(session._store.load(session.id).to_dict())
            assert secret not in (tmp_path / "tasks" / f"{session.id}.json").read_text()
            assert secret not in caplog.text
        finally:
            session.stop()
            await asyncio.gather(session._task, return_exceptions=True)
            await asyncio.to_thread(user_display.disconnect)


async def test_vnc_errors_timeouts_and_reconnect_do_not_leave_connections_or_stale_results(desktop):
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    count_script = """from pathlib import Path
count=0
for name in ('tcp','tcp6'):
 for line in Path('/proc/net/'+name).read_text().splitlines()[1:]:
  fields=line.split()
  count += fields[1].endswith(':170C') and fields[3]=='01'
print(count)
"""

    def connections():
        result = desktop.exec_run(["python3", "-c", count_script])
        assert result.exit_code == 0, result.output.decode()
        return int(result.output)

    async def disconnected():
        async with asyncio.timeout(3):
            while await asyncio.to_thread(connections):
                await asyncio.sleep(0.05)

    await disconnected()

    display = VNCClient()
    paused = False
    try:
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        with pytest.raises((TypeError, ValueError)):
            await asyncio.to_thread(display.key_down, "not-a-valid-key")
        await asyncio.to_thread(display.mouse_move, 10, 10)
        await asyncio.to_thread(display.disconnect)
        await disconnected()

        # A new handshake closes the old connection and clears a previous stop request.
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        display.request_stop()
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        display._check_stop()
        screen = await asyncio.to_thread(display.capture_raw)
        assert (screen.width, screen.height) == (1280, 800)

        display._client.timeout = 0.2
        desktop.pause()
        paused = True
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(display.capture_raw)
        assert not display.is_connected
        with pytest.raises(RuntimeError, match="接続されていません"):
            await asyncio.to_thread(display.mouse_move, 20, 20)
        desktop.unpause()
        paused = False
        await disconnected()

        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        restored = await asyncio.to_thread(display.capture_raw)
        assert restored.image_bytes.startswith(b"\x89PNG")
        assert (restored.width, restored.height) == (1280, 800)
    finally:
        if paused:
            desktop.unpause()
        await asyncio.to_thread(display.disconnect)
    await disconnected()


@pytest.mark.parametrize("mode", ["stall", "close"])
async def test_incomplete_vnc_handshake_releases_the_real_tcp_connection(
    vnc_runtime, monkeypatch, mode
):
    from twisted.internet.error import ConnectionDone, ConnectionLost
    from vncdotool import api

    connected, closed = asyncio.Event(), asyncio.Event()
    handlers = set()

    async def serve(reader, writer):
        handler = asyncio.current_task()
        handlers.add(handler)
        try:
            connected.set()
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            with contextlib.suppress(ConnectionResetError):
                await reader.readexactly(12)
                if mode == "stall":
                    assert await reader.read() == b""
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()
            handlers.discard(handler)
            closed.set()

    original_connect = api.connect

    def bounded_connect(*args, **kwargs):
        kwargs["timeout"] = 0.2
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(api, "connect", bounded_connect)
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    display = VNCClient()
    try:
        expected = (
            TimeoutError if mode == "stall" else (ConnectionDone, ConnectionLost, ConnectionError)
        )
        with pytest.raises(expected):
            await asyncio.to_thread(
                display.connect, "127.0.0.1", server.sockets[0].getsockname()[1]
            )
        assert connected.is_set() and not display.is_connected
        await asyncio.wait_for(closed.wait(), 1)
    finally:
        await asyncio.to_thread(display.disconnect)
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), 1)
