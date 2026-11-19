"""Real VNC handles keep their owner when binding overlaps SDK execution or close."""

import asyncio
import json
import os
import socket

import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.actions.primitives import Action, ActionType
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


@pytest.mark.parametrize("mode", ["prestart", "active", "closed", "close-failure"])
async def test_real_connections_cannot_be_adopted_after_admission_or_lost_after_disconnect_failure(
    desktop, tmp_path, monkeypatch, mode
):
    model, requests = FastAPI(), []
    entered, release = asyncio.Event(), asyncio.Event()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        requests.append(body["response_format"]["json_schema"]["name"])
        entered.set()
        await release.wait()
        return completion('{"intent":"inspect","constraints":[],"reasoning":"inspect"}').json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-display-binding-key",
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1",
        timeout=10,
    )
    original, replacement = VNCClient(), VNCClient()
    session = TaskSession(
        llm=provider,
        display=original,
        store=TaskStore(tmp_path / "records"),
        vm_id="desk-original",
        decision=None,
    )
    disconnect = original.disconnect
    run, proof = None, {"mode": mode}
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.01)
        desktop.reload()
        port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(original.connect, "127.0.0.1", port)
        await asyncio.to_thread(replacement.connect, "127.0.0.1", port)
        assert (await asyncio.to_thread(original.capture_raw)).width > 0
        if mode == "active":
            run = asyncio.create_task(session.run("元の画面を確認する"))
            await asyncio.wait_for(entered.wait(), 5)
        elif mode == "closed":
            await session.aclose()
        elif mode == "close-failure":
            attempts = []

            def fail_once():
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("display disconnect acknowledgement lost")
                disconnect()

            monkeypatch.setattr(original, "disconnect", fail_once)
        try:
            session.set_display(replacement)
        except Exception as error:
            proof["binding_error"] = type(error).__name__
        proof["current_is_original"] = session.display is original
        if mode == "prestart":
            assert await session.executor.execute(Action(ActionType.MOUSE_MOVE, {"x": 10, "y": 12}))
            assert (await asyncio.to_thread(replacement.capture_raw)).width > 0
        await asyncio.wait_for(session.aclose(), 5)
        if run is not None:
            (result,) = await asyncio.gather(run, return_exceptions=True)
            proof["run_cancelled"] = isinstance(result, asyncio.CancelledError)
        proof.update(
            original_connected=original.is_connected,
            replacement_connected=replacement.is_connected,
            requests=requests,
            provider_closed=provider._client.is_closed(),
        )
        for name, display in [("original", original), ("replacement", replacement)]:
            proof[name + "_readable"] = (
                display.is_connected and (await asyncio.to_thread(display.capture_raw)).width > 0
            )
        if mode == "active":
            record = session.snapshot()
            proof.update(state=record.state, stop_reason=record.stop_reason, vm_id=record.vm_id)
    finally:
        release.set()
        session.stop()
        if run is not None:
            await asyncio.gather(run, return_exceptions=True)
        await session.aclose()
        await asyncio.to_thread(disconnect)
        await asyncio.to_thread(replacement.disconnect)
        api.should_exit = True
        await asyncio.wait_for(serving, 5)
        (tmp_path / "display-binding-proof.json").write_text(json.dumps(proof, indent=2))
    assert (
        proof["provider_closed"]
        and not proof["original_connected"]
        and not proof["original_readable"]
    ), proof
    if mode == "prestart":
        assert "binding_error" not in proof and not proof["current_is_original"], proof
        assert not proof["replacement_connected"] and not proof["replacement_readable"], proof
    else:
        expected = "OSError" if mode == "close-failure" else "RuntimeError"
        assert proof.get("binding_error") == expected and proof["current_is_original"], proof
        assert proof["replacement_connected"] and proof["replacement_readable"], proof
    if mode == "active":
        assert proof["requests"] == ["understand_instruction"] and proof["run_cancelled"], proof
        assert proof["state"] == "failed" and proof["stop_reason"] == "cancelled", proof
        assert proof["vm_id"] == "desk-original", proof
    else:
        assert proof["requests"] == [], proof
