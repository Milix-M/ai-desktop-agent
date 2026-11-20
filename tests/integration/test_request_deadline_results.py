"""Late response processing must not allow real input, completion or another send."""

import asyncio
import json
import os
import socket
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
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


@pytest.mark.parametrize(
    "phase,response",
    [
        ("action_decision", "success"),
        ("verify_result", "success"),
        ("action_decision", "http-error"),
    ],
)
async def test_expired_response_processing_stops_before_guest_input_completion_or_retry(
    desktop,
    terminal,
    tmp_path,
    monkeypatch,
    phase,
    response,
    session_retry_sleep,
    provider_retry_sleep,
):
    display, proof = terminal
    calls, actions = [], 0
    delayed = asyncio.Event()
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        nonlocal actions
        if "response_format" not in body:
            return completion("{}").json()
        stage = body["response_format"]["json_schema"]["name"]
        calls.append(stage)
        if stage == "action_decision":
            actions += 1
        if stage == phase and response == "http-error":
            return JSONResponse({"error": {"message": "temporarily unavailable"}}, status_code=503)
        case = "action.valid" if phase == "action_decision" else "verify.valid"
        return completion(
            json.dumps(
                metadata_reply(
                    stage, case, f"printf forbidden > {proof}\n", action_number=actions or 1
                )
            )
        ).json()

    async def process_response(result):
        body = json.loads(result.request.content)
        stage = body.get("response_format", {}).get("json_schema", {}).get("name")
        if stage == phase:
            # Real TCP response is fully read; simulate CPU-bound consumer work.
            # No pending socket read can yield and hide the missed timer callback.
            await result.aread()
            delayed.set()
            time.sleep(0.4)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    await provider._client.close()
    provider._client = AsyncOpenAI(
        api_key="local-test-key",
        base_url=f"http://127.0.0.1:{port}/v1",
        max_retries=0,
        timeout=5,
        http_client=httpx.AsyncClient(event_hooks={"response": [process_response]}),
    )
    session = TaskSession(
        llm=provider,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=20, max_requests=20, request_timeout_seconds=0.2),
    )
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        await provider._call("initialize SDK")
        success = await session.run("inspect the report without entering text")
        exists = desktop.exec_run(["test", "-e", proof]).exit_code == 0
        record = TaskStore(tmp_path).load(session.id)
        (tmp_path / "request-deadline-proof.json").write_text(
            json.dumps(
                {
                    "run_success": success,
                    "forbidden_file_exists": exists,
                    "stop_reason": record.stop_reason,
                    "target_requests": calls.count(phase),
                    "request_stages": calls,
                },
                indent=2,
            )
        )
        assert delayed.is_set() and not exists and success is False
        assert record.state == "failed" and record.stop_reason == "request_timeout"
        assert record.current_subtask_index == 0 and calls.count(phase) == 1
        assert not display.is_connected
        provider_retry_sleep.assert_not_awaited()
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            result = await client.get(f"/tasks/{session.id}")
        assert result.status_code == 200 and result.json()["stop_reason"] == "request_timeout"
    finally:
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
