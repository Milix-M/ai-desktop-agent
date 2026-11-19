"""A real desktop frame cannot be sent with a false image MIME type."""

import asyncio
import base64
import io
import json
import os
import socket

import pytest
import uvicorn
from fastapi import FastAPI
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


async def test_actual_frame_codec_failure_does_not_send_or_charge_a_model_request(
    desktop, tmp_path, monkeypatch
):
    model, requests, wire = FastAPI(), [], []

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        requests.append(body)
        url = body["messages"][-1]["content"][1]["image_url"]["url"]
        prefix, content = url.split(",", 1)
        with Image.open(io.BytesIO(base64.b64decode(content))) as image:
            wire.append({"mime": prefix, "format": image.format})
        return completion('{"action_type":"mouse_move","params":{"x":1,"y":1}}').json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-codec-test-key",
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1",
        timeout=5,
    )
    display = VNCClient()
    proof = {}
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.01)
        desktop.reload()
        port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", port)
        frame = await asyncio.to_thread(display.capture_raw)
        save = Image.Image.save

        def unavailable(image, fp, format=None, **kwargs):
            if format == "JPEG":
                raise OSError("JPEG codec unavailable")
            return save(image, fp, format=format, **kwargs)

        monkeypatch.setattr(Image.Image, "save", unavailable)
        try:
            await provider.decide_next_action(Goal("inspect"), Subtask("s1", "inspect"), [], frame)
        except Exception as error:
            proof["error"] = type(error).__name__
            proof["cause"] = type(error.__cause__).__name__
        proof.update(
            call_count=provider.call_count,
            requests=len(requests),
            size=[frame.width, frame.height],
            wire=wire,
        )
    finally:
        await provider.aclose()
        await asyncio.to_thread(display.disconnect)
        api.should_exit = True
        await asyncio.wait_for(serving, 5)
        proof.update(
            provider_closed=provider._client.is_closed(), display_connected=display.is_connected
        )
        (tmp_path / "image-encoding-failure-proof.json").write_text(json.dumps(proof, indent=2))
    assert proof.get("error") == "ValueError" and proof.get("cause") == "OSError", proof
    assert proof["call_count"] == proof["requests"] == 0, proof
    assert proof["provider_closed"] and not proof["display_connected"], proof
