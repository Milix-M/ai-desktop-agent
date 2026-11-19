"""A successful real VNC/manual recovery keeps the original input failure separate."""

import asyncio
import json
import os
import socket

import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.test_manual_input import wait_for_manual
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


async def test_successful_manual_recovery_does_not_reuse_a_real_failed_key_error(desktop, tmp_path):
    requests = []
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        stage = body["response_format"]["json_schema"]["name"]
        requests.append(stage)
        if stage == "initial_plan":
            data = {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "subtasks": [
                    {
                        "id": "inspect",
                        "description": "inspect the document",
                        "expected_outcome": "the operator confirms the target",
                    }
                ],
                "reasoning": "inspect",
            }
        elif stage == "action_decision":
            data = {
                "action_type": "key_press",
                "params": {"key": "unknown-keysym"},
                "confidence": 1,
            }
            if requests.count("action_decision") > 1:
                data = {"action_type": "subtask_complete", "params": {}, "confidence": 1}
        elif stage == "recover_from_error":
            data = {
                "strategy": "alternative_approach",
                "recoverable": True,
                "actions": [
                    {
                        "action_type": "request_user_input",
                        "params": {"reason": "対象を確認してください"},
                    }
                ],
                "reasoning": "ask the operator to confirm the target",
            }
        elif stage == "verify_result":
            data = {
                "success": True,
                "reasoning": "operator confirmed",
                "evidence": "confirmed target",
            }
        else:
            raise AssertionError(stage)
        return completion(json.dumps(data)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-recovery-handoff-key",
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1",
        timeout=10,
    )
    display = VNCClient()
    store = TaskStore(tmp_path / "records")
    session = TaskSession(llm=provider, display=display, store=store, decision=None)
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.01)
        desktop.reload()
        port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        await session.start_async("対象を確認して作業を完了する")
        async with asyncio.timeout(10):
            consent = await wait_for_manual(session)
        paused_requests = list(requests)
        await asyncio.sleep(0.1)
        assert requests == paused_requests
        session.resume(manual_input_id=consent["id"], allow_screen_share=True)
        assert await asyncio.wait_for(session._task, 10) is True
        record = store.load(session.id)
        proof = {
            "state": record.state,
            "success": record.success,
            "actions": record.to_dict()["actions"],
            "requests": requests,
            "provider_closed": provider._client.is_closed(),
            "display_connected": display.is_connected,
        }
        (tmp_path / "recovery-handoff-proof.json").write_text(json.dumps(proof, indent=2))
        assert record.actions[0].action_type == "key_press" and not record.actions[0].success
        assert record.actions[0].error_message
        assert record.actions[1].action_type == "request_user_input" and record.actions[1].success
        assert record.actions[1].error_message == ""
        assert record.actions[2].action_type == "subtask_complete" and record.actions[2].success
        assert requests == [
            "initial_plan",
            "action_decision",
            "recover_from_error",
            "action_decision",
            "verify_result",
        ]
        assert proof["provider_closed"] and not proof["display_connected"]
    finally:
        session.stop()
        if session._task is not None:
            await asyncio.gather(session._task, return_exceptions=True)
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
