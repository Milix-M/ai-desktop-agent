"""The declared subtask time limit must stop late SDK responses and real input."""

import asyncio
import json
import os
import socket
from dataclasses import replace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.llm.types import DecompositionResult
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from tests.http import AuthenticatedClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.integration.test_model_metadata import terminal as terminal
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


class ShortStepProvider(OpenAICompatProvider):
    async def decompose_task(self, *args, **kwargs):
        result = await super().decompose_task(*args, **kwargs)
        # Exercise the public Subtask timeout without replacing the SDK/model flow.
        return DecompositionResult(
            [replace(subtask, timeout_seconds=1.0) for subtask in result.subtasks], result.reasoning
        )


@pytest.mark.parametrize("phase", ["decision", "verification", "recovery"])
async def test_a_late_model_response_cannot_type_or_complete_a_real_subtask(
    desktop, terminal, tmp_path, monkeypatch, phase, session_retry_sleep
):
    display, proof = terminal
    requests, actions, recoveries = [], 0, 0
    entered = asyncio.Event()
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        nonlocal actions, recoveries
        stage = body["response_format"]["json_schema"]["name"]
        requests.append(stage)
        if stage == "understand_instruction":
            data = {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "reasoning": "inspect",
            }
        elif stage == "decompose_task":
            data = {
                "subtasks": [
                    {
                        "id": "s1",
                        "description": "inspect the report",
                        "expected_outcome": "report visible",
                    }
                ],
                "reasoning": "inspect",
            }
        elif stage == "action_decision":
            actions += 1
            data = (
                {
                    "action_type": "type",
                    "params": {"text": f"printf forbidden > {proof}\n"},
                    "confidence": 1,
                    "reasoning": "late input",
                }
                if phase == "decision" and actions == 1
                else {
                    "action_type": "subtask_complete",
                    "params": {},
                    "confidence": 1,
                    "reasoning": "visible",
                }
            )
        elif stage == "verify_result":
            data = {
                "success": phase != "recovery" or recoveries > 0,
                "reasoning": "inspect",
                "evidence": "visible",
            }
        elif stage == "recover_from_error":
            recoveries += 1
            data = {
                "strategy": "alternative_approach",
                "actions": [
                    {"action_type": "type", "params": {"text": f"printf forbidden > {proof}\n"}}
                ],
                "reasoning": "late recovery",
                "recoverable": True,
            }
        else:
            raise AssertionError(stage)
        late_stage = {
            "decision": "action_decision",
            "verification": "verify_result",
            "recovery": "recover_from_error",
        }[phase]
        if stage == late_stage:
            entered.set()
            await asyncio.sleep(1.5)
        return completion(json.dumps(data)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = ShortStepProvider(
        api_key="local-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    session = TaskSession(
        llm=provider,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=20, max_requests=25),
    )
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        success = await session.run("inspect the report without entering text")
        exists = desktop.exec_run(["test", "-e", proof]).exit_code == 0
        saved = TaskStore(tmp_path).load(session.id)
        (tmp_path / "subtask-proof.json").write_text(
            json.dumps(
                {
                    "run_success": success,
                    "forbidden_file_exists": exists,
                    "stop_reason": saved.stop_reason,
                    "request_stages": requests,
                },
                indent=2,
            )
        )
        assert entered.is_set() and not exists and success is False
        assert saved.state == "failed" and saved.stop_reason == "subtask_timeout"
        assert "inspect the report" in saved.error_message and saved.current_subtask_index == 0
        assert all(action.action_type == "subtask_complete" for action in saved.actions)
        assert not display.is_connected
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            response = await client.get(f"/tasks/{session.id}")
        assert response.status_code == 200 and response.json()["stop_reason"] == "subtask_timeout"
    finally:
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
