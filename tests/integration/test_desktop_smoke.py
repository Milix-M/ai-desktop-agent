"""Real container, browser, and VNC checks. Opt in with RUN_DESKTOP_SMOKE=1."""

import asyncio
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

import docker
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, DecompositionResult, VerificationResult
from ai_desktop_agent.agent.state import Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_client import VNCClient

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
    container = client.containers.run(
        image,
        detach=True,
        ports={"5900/tcp": ("127.0.0.1", None)},
        shm_size="1g",
        labels={"ai-desktop-agent.smoke": uuid.uuid4().hex},
    )
    try:
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
        container.remove(force=True)
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
