"""Actual model/VNC success survives interrupted terminal event delivery."""

import asyncio
import json
import os
import shlex
import socket

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.state import AgentState
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


@pytest.mark.parametrize("phase", ["state", "complete"])
async def test_http_stop_during_terminal_delivery_keeps_the_real_result_and_one_completion(
    desktop, terminal, tmp_path, monkeypatch, phase, session_retry_sleep
):
    display, proof = terminal
    entered, released = asyncio.Event(), asyncio.Event()
    stages, states, completions = [], [], []
    actions = 0
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        nonlocal actions
        stage = body["response_format"]["json_schema"]["name"]
        stages.append(stage)
        if stage == "action_decision":
            actions += 1
        return completion(
            json.dumps(
                metadata_reply(
                    stage,
                    "action.valid",
                    f"printf terminal-result > {shlex.quote(proof)}\n",
                    action_number=actions or 1,
                )
            )
        ).json()

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
        vm_id="desk-terminal",
        decision=None,
        limits=ExecutionLimits(max_seconds=20),
    )

    async def changed(state, context):
        if state in {AgentState.COMPLETED, AgentState.FAILED}:
            states.append(state.value)
            if phase == "state":
                entered.set()
                await released.wait()

    async def complete(success):
        completions.append(success)
        if phase == "complete":
            entered.set()
            await released.wait()

    session.on_state_change(changed)
    session.on_complete(complete)
    monkeypatch.setattr(server, "_store", session._store)
    monkeypatch.setattr(server, "_sessions", {session.id: session})
    monkeypatch.setattr(server, "_active_session", session)
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        await session.start_async("write the verified terminal result")
        await asyncio.wait_for(entered.wait(), 10)
        assert desktop.exec_run(["cat", proof]).output == b"terminal-result"
        assert TaskStore(tmp_path).load(session.id).state == "completed"
        before = list(stages)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            result = await client.post(
                "/tasks/current/stop",
                params={"vm_id": session.vm_id, "session_id": session.id},
            )
        assert result.status_code == 200
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        record = TaskStore(tmp_path).load(session.id)
        result = {
            "state": record.state,
            "success": record.success,
            "stop_reason": record.stop_reason,
            "state_events": states,
            "completion_events": completions,
            "artifact": desktop.exec_run(["cat", proof]).output.decode(),
            "requests_after_stop": len(stages) - len(before),
        }
        (tmp_path / "terminal-delivery-proof.json").write_text(json.dumps(result, indent=2))
        assert record.state == "completed" and record.success is True
        assert record.stop_reason is None
        assert states == ["completed"] and completions == [True]
        assert result["artifact"] == "terminal-result" and result["requests_after_stop"] == 0
        assert not display.is_connected
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            detail = await client.get(f"/tasks/{session.id}")
        assert detail.status_code == 200 and detail.json()["success"] is True
        assert detail.json()["stop_reason"] is None
    finally:
        released.set()
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
