"""Real container, browser, and VNC checks. Opt in with RUN_DESKTOP_SMOKE=1."""

import asyncio
import os
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


async def test_scripted_session_writes_a_file_via_real_vnc(desktop, tmp_path):
    output_path = f"/tmp/desktop-smoke-{uuid.uuid4().hex}"
    desktop.exec_run(
        [
            "xfce4-terminal",
            "--disable-server",
            "--title=Desktop smoke",
            "--command=bash --noprofile --norc",
        ],
        user="agent",
        environment={"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"},
        detach=True,
    )
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    for _ in range(60):
        found = desktop.exec_run(
            ["xdotool", "search", "--onlyvisible", "--name", "^Desktop smoke$"],
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
                    Action(ActionType.TYPE, {"text": f"printf desktop-smoke-ok > {output_path}"}),
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
                result.exit_code == 0 and result.output == b"desktop-smoke-ok",
                "file check",
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
    assert desktop.exec_run(["cat", output_path]).output == b"desktop-smoke-ok"
    record = store.load(session.id)
    assert record.state == "completed" and record.success
    assert len(record.actions) == 3
    assert all(action.duration_ms > 0 for action in record.actions)
