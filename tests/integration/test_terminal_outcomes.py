"""Controlled failures persist their cause and stop real desktop input at limits."""

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
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("empty-plan", "planning_failed"),
        ("unmet", "verification_failed"),
        ("give-up", "recovery_failed"),
        ("replan-limit", "replan_limit"),
    ],
)
async def test_failed_outcome_is_durable_and_cannot_type_past_the_replan_limit(
    desktop, tmp_path, monkeypatch, outcome, code, session_retry_sleep
):
    token = uuid.uuid4().hex
    title, proof = "Terminal outcome " + token, "/tmp/outcome-" + token
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

    requests, recoveries = [], []
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        stage = body["response_format"]["json_schema"]["name"]
        requests.append(stage)
        if stage in ("initial_plan", "decompose_task"):
            data = {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "subtasks": []
                if outcome == "empty-plan"
                else [
                    {
                        "id": "s1",
                        "description": "inspect the report",
                        "expected_outcome": "report visible",
                    }
                ],
                "reasoning": "no valid steps terminal-local-test-key",
            }
        elif stage == "action_decision":
            data = {"action_type": "subtask_complete", "params": {}, "confidence": 1}
        elif stage == "verify_result":
            data = {
                "success": False,
                "reasoning": "report unavailable terminal-local-test-key",
                "evidence": "report missing",
            }
        elif stage == "recover_from_error":
            recoveries.append(body)
            data = {
                "strategy": "replan_subtask"
                if outcome == "replan-limit"
                else "give_up"
                if outcome == "give-up"
                else "alternative_approach",
                "actions": [
                    {"action_type": "type", "params": {"text": f"printf forbidden > {proof}\n"}}
                ]
                if outcome == "replan-limit" and len(recoveries) == 4
                else [],
                "reasoning": "permission unavailable terminal-local-test-key",
                "recoverable": outcome != "give-up",
            }
        else:
            raise AssertionError(stage)
        return completion(json.dumps(data)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="terminal-local-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    display = VNCClient()
    store = TaskStore(tmp_path / "records")
    session = TaskSession(
        llm=provider,
        display=display,
        store=store,
        decision=None,
        limits=ExecutionLimits(max_requests=40, max_seconds=40),
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
        assert await session.run("inspect the report without changing the desktop") is False
        exists = desktop.exec_run(["test", "-e", proof]).exit_code == 0
        record = TaskStore(tmp_path / "records").load(session.id)
        (tmp_path / "outcome-proof.json").write_text(
            json.dumps(
                {"forbidden_file_exists": exists, "stop_reason": record.stop_reason}, indent=2
            )
        )
        assert not exists
        assert record.state == "failed" and record.success is False and record.stop_reason == code
        assert record.error_message and "terminal-local-test-key" not in record.error_message
        if outcome != "replan-limit":
            assert "[REDACTED]" in record.error_message
        if outcome == "replan-limit":
            assert len(recoveries) == 4
        assert all(action.action_type == "subtask_complete" for action in record.actions)
        assert not display.is_connected
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path / "records"))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            detail = (await client.get(f"/tasks/{session.id}")).json()
        assert detail["stop_reason"] == code and detail["error_message"] == record.error_message
    finally:
        (tmp_path / "model-requests.json").write_text(json.dumps(requests))
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            desktop.exec_run(["xdotool", "windowclose", window], user="agent", environment=env)
