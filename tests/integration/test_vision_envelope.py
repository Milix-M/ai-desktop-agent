"""Actual VNC frames survive SDK JSON fallback without blocking concurrent calls."""

import asyncio
import base64
import hashlib
import io
import json
import os
import socket
import threading

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image

from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


async def test_actual_frame_fallback_and_encoding_cancellation_preserve_the_request_contract(
    desktop, tmp_path
):
    model = FastAPI()
    requests = []

    @model.post("/v1/chat/completions")
    async def respond(request: Request):
        body = await request.json()
        content = body["messages"][1]["content"]
        entry = {
            "schema": "response_format" in body,
            "session": request.headers.get("x-opencode-session"),
            "image": isinstance(content, list),
        }
        if isinstance(content, list):
            assert [p["type"] for p in content] == ["text", "image_url"]
            picture = content[1]["image_url"]
            prefix, payload = picture["url"].split(",", 1)
            assert prefix == "data:image/jpeg;base64" and picture["detail"] == "high"
            data = base64.b64decode(payload)
            with Image.open(io.BytesIO(data)) as image:
                entry.update(size=list(image.size), format=image.format)
            entry["image_sha256"] = hashlib.sha256(data).hexdigest()
            prompt = content[0]["text"]
        else:
            prompt = content
        entry["instructions"] = prompt.count("必ずJSONオブジェクトのみ")
        requests.append(entry)
        if len(requests) == 1:
            return JSONResponse(
                {
                    "error": {
                        "message": "response_format json_schema is not supported",
                        "param": "response_format",
                        "code": "unsupported_parameter",
                    }
                },
                status_code=400,
            )
        result = (
            {"action_type": "mouse_move", "params": {"x": 10, "y": 12}, "confidence": 1}
            if entry["image"]
            else {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "reasoning": "inspect",
            }
        )
        return completion(json.dumps(result)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="vision-native-test-key",
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1",
        timeout=10,
        session_id="native-vision-envelope",
    )
    display = VNCClient()
    entered, release = threading.Event(), threading.Event()
    pending, fallback = None, None
    proof = {}
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.01)
        desktop.reload()
        port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        frame = await asyncio.to_thread(display.capture_raw)
        goal, step = (
            Goal("inspect the visible target"),
            Subtask("inspect", "inspect the visible target"),
        )
        for _ in range(2):
            decision = await provider.decide_next_action(goal, step, [], frame)
            assert decision.action.params == {"x": 10, "y": 12}
        assert len(requests) == 3
        assert [r["schema"] for r in requests] == [True, False, False]
        assert [r["instructions"] for r in requests] == [0, 1, 1]
        assert all(
            r["size"] == [frame.width, frame.height] and r["format"] == "JPEG" for r in requests
        )
        assert len({r["image_sha256"] for r in requests}) == 1
        encode = provider._to_jpeg

        def controlled_encode(data):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("controller did not release image encoding")
            return encode(data)

        provider._to_jpeg = controlled_encode
        pending = asyncio.create_task(provider.decide_next_action(goal, step, [], frame))
        fallback = threading.Timer(3, release.set)
        fallback.start()
        assert await asyncio.to_thread(entered.wait, 2)
        proof["controller_live_during_encoding"] = not release.is_set()
        assert proof["controller_live_during_encoding"]
        assert (
            await provider.understand_instruction(Goal("inspect separately"))
        ).intent == "inspect"
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        proof.update(
            requests=requests, call_count=provider.call_count, size=[frame.width, frame.height]
        )
        assert len(requests) == provider.call_count == 4
        assert requests[-1]["image"] is False
        assert all(r["session"] == "native-vision-envelope" for r in requests)
    finally:
        if fallback is not None:
            fallback.cancel()
        release.set()
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await provider.aclose()
        await asyncio.to_thread(display.disconnect)
        proof.update(
            requests=requests,
            call_count=provider.call_count,
            provider_closed=provider._client.is_closed(),
            display_connected=display.is_connected,
        )
        (tmp_path / "vision-envelope-proof.json").write_text(json.dumps(proof, indent=2))
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
