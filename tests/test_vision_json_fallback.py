"""JSON fallback preserves the actual SDK's image envelope and cached capability."""

import asyncio
import base64
import io
import json
import threading

import httpx
import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import ActionType
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot
from tests.test_task_budget import completion, llm_with_transport


@pytest.mark.parametrize("initial_mode", ["text", "vision"])
async def test_sdk_json_fallback_preserves_images_and_later_cached_vision_requests(
    initial_mode, provider_retry_sleep
):
    frames, headers = [], []

    def handle(request):
        frames.append(json.loads(request.content))
        headers.append(request.headers)
        if len(frames) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "response_format json_schema is not supported",
                        "param": "response_format",
                        "code": "unsupported_parameter",
                    }
                },
            )
        data = {
            "action_type": "mouse_move",
            "params": {"x": 10, "y": 12},
            "confidence": 0.9,
            "reasoning": "inspect the visible target",
        }
        if initial_mode == "text" and len(frames) == 2:
            data = {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "reasoning": "inspect first",
            }
        return completion(json.dumps(data))

    image = Image.new("RGB", (32, 24), (80, 120, 200))
    image.putpixel((10, 12), (250, 20, 60))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    screenshot = Screenshot(encoded.getvalue(), 32, 24)
    provider = await llm_with_transport(handle)
    provider.session_id = "vision-fallback-session"
    goal = Goal("inspect the visible target")
    subtask = Subtask("inspect", "inspect the visible target")
    try:
        if initial_mode == "text":
            assert (await provider.understand_instruction(goal)).intent == "inspect"
        else:
            assert (
                await provider.decide_next_action(goal, subtask, [], screenshot)
            ).action.action_type == ActionType.MOUSE_MOVE
        assert (await provider.decide_next_action(goal, subtask, [], screenshot)).action.params == {
            "x": 10,
            "y": 12,
        }
        assert len(frames) == provider.call_count == 3
        assert "response_format" in frames[0]
        assert all("response_format" not in f for f in frames[1:])
        assert all(h["x-opencode-session"] == "vision-fallback-session" for h in headers)
        urls = []
        for index, frame in enumerate(frames):
            content = frame["messages"][1]["content"]
            if initial_mode == "text" and index < 2:
                assert isinstance(content, str)
                prompt = content
            else:
                assert [part["type"] for part in content] == ["text", "image_url"]
                prompt = content[0]["text"]
                picture = content[1]["image_url"]
                assert picture["detail"] == "high"
                prefix, payload = picture["url"].split(",", 1)
                assert prefix == "data:image/jpeg;base64"
                with (
                    Image.open(io.BytesIO(base64.b64decode(payload))) as decoded,
                    screenshot.to_pil() as original,
                ):
                    assert decoded.format == "JPEG" and decoded.size == original.size
                urls.append(picture["url"])
            assert prompt.count("必ずJSONオブジェクトのみ") == (0 if index == 0 else 1)
        assert len(set(urls)) == 1
        provider_retry_sleep.assert_not_awaited()
    finally:
        await provider.aclose()


@pytest.mark.parametrize("mode", ["cancel", "parallel-text"])
async def test_pending_image_encoding_does_not_block_other_calls_or_send_after_cancellation(mode):
    entered, release = threading.Event(), threading.Event()
    sent = []

    def handle(request):
        body = json.loads(request.content)
        sent.append(body)
        stage = body["response_format"]["json_schema"]["name"]
        data = (
            {
                "intent": "inspect",
                "target_application": None,
                "constraints": [],
                "reasoning": "inspect",
            }
            if stage == "understand_instruction"
            else {"action_type": "mouse_move", "params": {"x": 10, "y": 12}, "confidence": 1}
        )
        return completion(json.dumps(data))

    provider = await llm_with_transport(handle)
    encode = provider._to_jpeg

    def controlled_encode(data):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("controller did not release image encoding")
        return encode(data)

    provider._to_jpeg = controlled_encode
    image = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(image, format="PNG")
    operation = asyncio.create_task(
        provider.decide_next_action(
            Goal("inspect"),
            Subtask("s1", "inspect"),
            [],
            Screenshot(image.getvalue(), 32, 24),
        )
    )
    fallback = threading.Timer(2, release.set)
    fallback.start()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not release.is_set(), "image conversion blocked the event-loop controller"
        if mode == "cancel":
            operation.cancel()
            await asyncio.sleep(0)
            assert sent == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 2)
            assert sent == [] and provider.call_count == 0
        else:
            assert (
                await provider.understand_instruction(Goal("inspect separately"))
            ).intent == "inspect"
            assert len(sent) == 1 and isinstance(sent[0]["messages"][1]["content"], str)
            release.set()
            assert (
                await asyncio.wait_for(operation, 2)
            ).action.action_type == ActionType.MOUSE_MOVE
            assert provider.call_count == len(sent) == 2
    finally:
        fallback.cancel()
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await provider.aclose()
