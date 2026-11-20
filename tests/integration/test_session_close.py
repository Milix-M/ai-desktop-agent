"""Closing an active real SDK/VNC session must drain its task before returning."""

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
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize("mode", ["direct", "background"])
async def test_close_cancels_the_actual_model_request_and_releases_vnc_before_return(
    desktop, tmp_path, monkeypatch, mode
):
    entered, release = asyncio.Event(), asyncio.Event()
    requests, completions = [], []
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        requests.append(body["response_format"]["json_schema"]["name"])
        entered.set()
        await release.wait()
        return completion(
            json.dumps(
                {
                    "intent": "inspect",
                    "target_application": None,
                    "constraints": [],
                    "reasoning": "inspect the saved document",
                }
            )
        ).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-session-close-key",
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1",
        timeout=10,
    )
    desktop.reload()
    port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
    monkeypatch.setenv("VNC_HOST", "127.0.0.1")
    monkeypatch.setenv("VNC_PORT", str(port))
    monkeypatch.delenv("VNC_PASSWORD", raising=False)
    display = VNCClient() if mode == "direct" else None
    store = TaskStore(tmp_path / "records")
    session = TaskSession(llm=provider, display=display, store=store, decision=None)
    display = session.display
    session.on_complete(completions.append)
    run = None
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.01)
        if mode == "direct":
            await asyncio.to_thread(display.connect, "127.0.0.1", port)
        screenshot = await asyncio.to_thread(display.capture_raw)
        assert screenshot.width > 0 and screenshot.height > 0
        if mode == "direct":
            run = asyncio.create_task(session.run("保存済みの文書を確認する"))
        else:
            await session.start_async("保存済みの文書を確認する")
            run = session._task
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(session.aclose(), 5)
        record = store.load(session.id)
        proof = {
            "mode": mode,
            "run_done": run.done(),
            "running": session.is_running,
            "display_connected": display.is_connected,
            "provider_closed": provider._client.is_closed(),
            "state": record.state,
            "stop_reason": record.stop_reason,
            "action_count": len(record.actions),
            "completions": completions,
            "requests": requests,
        }
        (tmp_path / "session-close-proof.json").write_text(json.dumps(proof, indent=2))
        assert proof["run_done"] and not proof["running"], proof
        assert not proof["display_connected"] and proof["provider_closed"], proof
        assert proof["state"] == "failed" and proof["stop_reason"] == "cancelled", proof
        assert proof["action_count"] == 0 and completions == [False], proof
        release.set()
        await asyncio.gather(run, return_exceptions=True)
        assert requests == ["initial_plan"]
    finally:
        session.stop()
        release.set()
        if run is not None:
            await asyncio.gather(run, return_exceptions=True)
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
