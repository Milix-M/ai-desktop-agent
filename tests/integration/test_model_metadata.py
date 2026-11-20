"""Valid JSON with bad metadata must neither type nor authorize completion."""

import asyncio
import json
import os
import socket
import uuid

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.http import AuthenticatedClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.test_model_metadata import metadata_reply
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.fixture
async def terminal(desktop):
    token = uuid.uuid4().hex
    title, proof = "Metadata " + token, "/tmp/metadata-" + token
    ready = proof + "-ready"
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}
    display, window = VNCClient(), None
    try:
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
                await asyncio.sleep(0.05)
        desktop.reload()
        port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        await asyncio.to_thread(display.type_text, f"printf ready > {ready}\n")
        async with asyncio.timeout(5):
            while desktop.exec_run(["cat", ready]).output != b"ready":
                await asyncio.sleep(0.05)
        yield display, proof
    finally:
        await asyncio.to_thread(display.disconnect)
        if window is not None:
            desktop.exec_run(["xdotool", "windowclose", window], user="agent", environment=env)


@pytest.mark.parametrize(
    "case", ["plan.id-duplicate", "action.reasoning", "verify.reasoning", "recover.reasoning"]
)
async def test_metadata_is_rejected_before_real_input_or_completion(
    desktop, terminal, tmp_path, monkeypatch, case, session_retry_sleep
):
    display, proof = terminal
    requests, actions = [], 0
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        nonlocal actions
        stage = body["response_format"]["json_schema"]["name"]
        requests.append(stage)
        if stage == "action_decision":
            actions += 1
        data = metadata_reply(
            stage, case, f"printf forbidden > {proof}\n", action_number=actions or 1
        )
        return completion(json.dumps(data)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    session = TaskSession(
        llm=provider,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_requests=25, max_seconds=20),
    )
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        success = await session.run("inspect the report without entering text")
        exists = desktop.exec_run(["test", "-e", proof]).exit_code == 0
        record = TaskStore(tmp_path).load(session.id)
        (tmp_path / "metadata-proof.json").write_text(
            json.dumps(
                {
                    "run_success": success,
                    "forbidden_file_exists": exists,
                    "history_readable": record is not None,
                    "stop_reason": record.stop_reason if record else None,
                    "request_stages": requests,
                },
                indent=2,
            )
        )
        assert not exists and success is False
        assert record is not None and record.state == "failed" and record.success is False
        assert record.stop_reason == "execution_error" and record.error_message
        assert case.split(".")[1].split("-")[0] in record.error_message
        assert all(action.action_type == "subtask_complete" for action in record.actions)
        assert not display.is_connected
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            response = await client.get(f"/tasks/{session.id}")
        assert response.status_code == 200
        assert response.json()["error_message"] == record.error_message
    finally:
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
