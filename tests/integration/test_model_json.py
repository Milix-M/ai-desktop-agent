"""Real SDK HTTP responses cannot turn ambiguous model JSON into desktop input."""

import asyncio
import json
import os
import socket
import uuid

import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize("failure", ["duplicate-key", "array-wrapper"])
async def test_invalid_model_json_cannot_write_to_the_actual_desktop(
    desktop, tmp_path, failure, provider_retry_sleep, session_retry_sleep
):
    token = uuid.uuid4().hex
    title = "Model JSON " + token
    proof = "/tmp/model-json-" + token
    preflight = proof + "-ready"
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    desktop.exec_run(
        ["xfce4-terminal", "--disable-server", f"--title={title}"],
        detach=True,
        user="agent",
        environment=env,
    )
    async with asyncio.timeout(10):
        while True:
            found = desktop.exec_run(
                ["xdotool", "search", "--onlyvisible", "--name", title],
                user="agent",
                environment=env,
            )
            if found.exit_code == 0:
                window = found.output.decode().splitlines()[0]
                desktop.exec_run(
                    ["xdotool", "windowactivate", window], user="agent", environment=env
                )
                break
            await asyncio.sleep(0.1)
    command = f"printf forbidden > {proof}\n"
    if failure == "duplicate-key":
        content = (
            '{"action_type":"type","params":{"text":"","text":'
            + json.dumps(command)
            + '},"confidence":1}'
        )
    else:
        content = json.dumps(
            [False, {"action_type": "type", "params": {"text": command}, "confidence": 1}]
        )
    requests = []
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        requests.append(body)
        return completion(content).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-model-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    display = VNCClient()
    session = TaskSession(llm=provider, display=display, decision=None)
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        desktop.reload()
        vnc_port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", vnc_port)
        await asyncio.to_thread(display.type_text, f"printf ready > {preflight}\n")
        async with asyncio.timeout(5):
            while desktop.exec_run(["cat", preflight]).output != b"ready":
                await asyncio.sleep(0.1)
        session.loop.start(Goal("不正な応答から入力しない"))
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask("s1", "入力を確認する")])
        error = None
        try:
            await session._execute_phase()
        except ValueError as caught:
            error = caught
        assert desktop.exec_run(["test", "-e", proof]).exit_code != 0
        assert error is not None
        assert session.loop.context.action_history == []
        assert len(requests) == 3
        assert [call.args for call in provider_retry_sleep.await_args_list] == [(1.5,), (3.0,)]
        session_retry_sleep.assert_not_awaited()
        (tmp_path / "model-requests.json").write_text(json.dumps(requests, ensure_ascii=False))
    finally:
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            desktop.exec_run(["xdotool", "windowclose", window], user="agent", environment=env)
