"""Operator stop remains authoritative when both a provider and transport misbehave."""

import asyncio
import json
import os
import socket
from unittest.mock import Mock

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, VerificationResult
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from tests.http import AuthenticatedClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.integration.test_model_metadata import terminal as terminal
from tests.test_model_metadata import metadata_reply
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize("phase", ["action", "verification"])
async def test_stop_outlives_a_cancel_suppressing_sdk_wrapper_and_failed_vnc_stop(
    desktop, terminal, tmp_path, monkeypatch, phase, session_retry_sleep
):
    display, proof = terminal
    entered = asyncio.Event()
    calls, actions = [], 0
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        nonlocal actions
        stage = body["response_format"]["json_schema"]["name"]
        calls.append(stage)
        if stage == "action_decision":
            actions += 1
        if (phase == "action" and stage == "action_decision" and actions == 1) or (
            phase == "verification" and stage == "verify_result"
        ):
            entered.set()
            await asyncio.sleep(0.5)
        return completion(
            json.dumps(metadata_reply(stage, "verify.valid", action_number=actions or 1))
        ).json()

    class Provider(OpenAICompatProvider):
        async def decide_next_action(self, *args, **kwargs):
            try:
                return await super().decide_next_action(*args, **kwargs)
            except asyncio.CancelledError:
                return ActionDecision(
                    Action(ActionType.TYPE, {"text": f"printf forbidden > {proof}\n"}),
                    "",
                    1,
                    "late fallback",
                )

        async def verify_result(self, *args, **kwargs):
            try:
                return await super().verify_result(*args, **kwargs)
            except asyncio.CancelledError:
                return VerificationResult(True, "late success", "late")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = Provider(api_key="local-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5)
    session = TaskSession(
        llm=provider,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=15),
    )
    monkeypatch.setattr(
        display, "request_stop", Mock(side_effect=OSError("injected VNC stop failure"))
    )
    completed = []
    session.on_complete(completed.append)
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        await session.start_async("inspect the report without entering text")
        await asyncio.wait_for(entered.wait(), 5)
        session.stop()
        cancelled, success = False, None
        try:
            success = await session._task
        except asyncio.CancelledError:
            cancelled = True
        exists = desktop.exec_run(["test", "-e", proof]).exit_code == 0
        record = TaskStore(tmp_path).load(session.id)
        (tmp_path / "stop-proof.json").write_text(
            json.dumps(
                {
                    "operator_stop_propagated": cancelled,
                    "run_success": success,
                    "forbidden_file_exists": exists,
                    "stop_reason": record.stop_reason,
                    "request_stages": calls,
                },
                indent=2,
            )
        )
        assert cancelled and not exists
        assert (
            record.state == "failed"
            and record.success is False
            and record.stop_reason == "cancelled"
        )
        assert record.current_subtask_index == 0 and completed == [False]
        assert all(action.action_type == "subtask_complete" for action in record.actions)
        assert not display.is_connected
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            result = await client.get(f"/tasks/{session.id}")
        assert result.status_code == 200 and result.json()["stop_reason"] == "cancelled"
    finally:
        if session.is_running:
            session.stop()
        if session._task:
            await asyncio.gather(session._task, return_exceptions=True)
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
