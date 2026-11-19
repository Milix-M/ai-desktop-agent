"""A failed completion service must not restart the task and type into a real guest."""

import asyncio
import json
import os
import socket
import uuid

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize("failure,expected_verifications", [(401, 1), (503, 3), ("malformed", 3)])
async def test_completion_service_failure_stops_before_another_gui_action_and_keeps_its_cause(
    desktop, tmp_path, failure, expected_verifications, provider_retry_sleep, session_retry_sleep
):
    token = uuid.uuid4().hex
    title, proof = "Verification " + token, "/tmp/verification-" + token
    ready = proof + "-ready"
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
            await asyncio.sleep(0.05)

    requests, verification_requests = [], []
    actions = []
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        requests.append(body)
        stage = body["response_format"]["json_schema"]["name"]
        if stage == "initial_plan":
            data = {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "subtasks": [
                    {
                        "id": "s1",
                        "description": "inspect the visible result",
                        "expected_outcome": "result visible",
                    }
                ],
                "reasoning": "inspect",
            }
        elif stage == "action_decision":
            actions.append(body)
            # If the session incorrectly restarts after the failed verification,
            # the next model decision would really type into the guest terminal.
            data = (
                {
                    "action_type": "type",
                    "params": {"text": f"printf forbidden > {proof}\n"},
                    "confidence": 1,
                }
                if len(actions) == 4
                else {"action_type": "subtask_complete", "params": {}, "confidence": 1}
            )
        elif stage == "verify_result":
            verification_requests.append(body)
            if failure == "malformed":
                return completion('[false,{"success":true}]').json()
            return JSONResponse(
                {
                    "error": {
                        "message": "verification rejected completion-local-test-key",
                        "code": "verification_unavailable",
                    }
                },
                status_code=failure,
            )
        else:
            return JSONResponse(
                {"error": {"message": "unexpected recovery request"}}, status_code=400
            )
        return completion(json.dumps(data)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="completion-local-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    display = VNCClient()
    store = TaskStore(tmp_path / "records")
    session = TaskSession(
        llm=provider,
        display=display,
        store=store,
        decision=None,
        limits=ExecutionLimits(max_requests=20, max_seconds=30),
    )
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        desktop.reload()
        vnc_port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", vnc_port)
        await asyncio.to_thread(display.type_text, f"printf ready > {ready}\n")
        async with asyncio.timeout(5):
            while desktop.exec_run(["cat", ready]).output != b"ready":
                await asyncio.sleep(0.05)
        assert await session.run("inspect the visible result without changing the desktop") is False
        exists = desktop.exec_run(["test", "-e", proof]).exit_code == 0
        record = store.load(session.id)
        (tmp_path / "failure-proof.json").write_text(
            json.dumps(
                {
                    "forbidden_file_exists": exists,
                    "verification_requests": len(verification_requests),
                    "action_requests": len(actions),
                    "stop_reason": record.stop_reason,
                },
                indent=2,
            )
        )
        assert not exists
        assert len(verification_requests) == expected_verifications
        assert len(actions) == 3
        assert provider.call_count == len(requests) == 4 + expected_verifications
        assert record.state == "failed" and record.stop_reason == "execution_error"
        assert record.error_message and "completion-local-test-key" not in record.error_message
        assert len(record.actions) == 1 and record.actions[0].action_type == "subtask_complete"
        assert [call.args for call in provider_retry_sleep.await_args_list] == (
            [(1.5,), (3.0,)] if expected_verifications == 3 else []
        )
    finally:
        (tmp_path / "model-requests.json").write_text(json.dumps(requests, ensure_ascii=False))
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            desktop.exec_run(["xdotool", "windowclose", window], user="agent", environment=env)
