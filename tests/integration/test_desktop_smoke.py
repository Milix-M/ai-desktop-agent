"""Real container, browser, and VNC checks. Opt in with RUN_DESKTOP_SMOKE=1."""

import asyncio
import os
import shlex
import subprocess
import time
import uuid

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
