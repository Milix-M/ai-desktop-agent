"""Actual SDK, cropped images and guest pointer state exercise precision guards."""

import asyncio
import json
import os
import socket
from dataclasses import asdict

import pytest
import uvicorn
from fastapi import FastAPI

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.screenshot import Screenshot
from ai_desktop_agent.vm.vnc_client import VNCClient
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.test_task_budget import completion

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize(
    "mode",
    [
        "low-confidence",
        "repeat-depth",
        "crop-boundary",
        "repeat-radius",
        "outside-radius",
        "different-button",
        "old-click",
        "thin-crop",
        "nested-thin-crop",
    ],
)
async def test_precision_failure_does_not_move_the_real_pointer_and_safe_crop_coordinates_do(
    desktop, tmp_path, mode
):
    initial = (
        {
            "action_type": "region_select",
            "params": {"x": 100, "y": 80, "width": 200, "height": 180},
            "confidence": 1,
        }
        if mode == "crop-boundary"
        else {
            "action_type": "left_click",
            "params": {"x": 200, "y": 180},
            "confidence": 0.2 if mode == "low-confidence" else 1,
        }
    )
    if mode in {"thin-crop", "nested-thin-crop"}:
        initial = {
            "action_type": "region_select",
            "params": {"x": 1277, "y": 80, "width": 3, "height": 500},
            "confidence": 1,
        }
    following = (
        {
            "action_type": "region_select",
            "params": {"x": 0, "y": 0, "width": 100, "height": 100},
            "confidence": 1,
        }
        if mode == "repeat-depth"
        else {
            "action_type": "left_click",
            "params": {
                "x": 50000 if mode == "crop-boundary" else 20 if mode == "repeat-radius" else 50,
                "y": 20,
            },
            "confidence": 1 if mode in {"crop-boundary", "repeat-radius"} else 0.2,
        }
    )
    if mode in {"thin-crop", "nested-thin-crop"}:
        following = {"action_type": "mouse_move", "params": {"x": 0, "y": 128}, "confidence": 1}
    requests = []
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def respond(body: dict):
        requests.append(body)
        prompt = body["messages"][-1]["content"]
        if isinstance(prompt, str):
            data = {
                "strategy": "give_up",
                "actions": [],
                "reasoning": "cannot locate target",
                "recoverable": False,
            }
        else:
            data = initial if len(requests) == 1 else following
            if mode == "nested-thin-crop" and len(requests) == 2:
                data = {
                    "action_type": "region_select",
                    "params": {"x": 0, "y": 64, "width": 1, "height": 128},
                    "confidence": 1,
                }
        return completion(json.dumps(data)).json()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(model, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    provider = OpenAICompatProvider(
        api_key="local-precision-test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
    )
    display = VNCClient()
    session = TaskSession(llm=provider, display=display, decision=None)
    env = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority"}

    def pointer():
        result = desktop.exec_run(
            ["xdotool", "getmouselocation", "--shell"], user="agent", environment=env
        )
        assert result.exit_code == 0, result.output.decode()
        values = dict(line.split("=", 1) for line in result.output.decode().splitlines())
        return int(values["X"]), int(values["Y"])

    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.05)
        desktop.reload()
        vnc_port = int(desktop.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await asyncio.to_thread(display.connect, "127.0.0.1", vnc_port)
        if mode in {"thin-crop", "nested-thin-crop"}:

            class LimitedScreenshot(Screenshot):
                def crop_with_meta(self, *args, **kwargs):
                    # A display adapter caps image resolution before model transport.
                    return super().crop_with_meta(*args, **(kwargs | {"max_dim": 256}))

            capture = display.capture_raw

            def limited_capture():
                return LimitedScreenshot(**asdict(capture()))

            display.capture_raw = limited_capture
        await asyncio.to_thread(display.mouse_move, 40, 40)
        async with asyncio.timeout(5):
            while pointer() != (40, 40):
                await asyncio.sleep(0.05)
        session.loop.start(Goal("select the visible target"))
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask("s1", "select the target")])
        if mode == "repeat-depth":
            session.loop.record_action(Action(ActionType.LEFT_CLICK, {"x": 200, "y": 180}), True)
        histories = {
            "repeat-radius": [Action(ActionType.LEFT_CLICK, {"x": 192, "y": 172})],
            "outside-radius": [Action(ActionType.LEFT_CLICK, {"x": 191, "y": 172})],
            "different-button": [
                Action(ActionType.RIGHT_CLICK, {"x": 200, "y": 180}),
                Action(ActionType.KEY_PRESS, {"key": "escape"}),
            ],
            "old-click": [
                Action(ActionType.LEFT_CLICK, {"x": 200, "y": 180}),
                *[Action(ActionType.MOUSE_MOVE, {"x": x, "y": 40}) for x in [10, 20, 30]],
            ],
        }
        for prior in histories.get(mode, []):
            assert await session.executor.execute(prior)
            session.loop.record_action(prior, True)
        if mode in histories:
            await asyncio.to_thread(display.mouse_move, 40, 40)
            async with asyncio.timeout(5):
                while pointer() != (40, 40):
                    await asyncio.sleep(0.05)
        previous = list(session.loop.context.action_history)
        await session._execute_phase()
        after = pointer()
        (tmp_path / "pointer-state.json").write_text(
            json.dumps({"before": [40, 40], "after": after})
        )
        if mode == "crop-boundary":
            assert after == (299, 90)
            assert session.loop.context.action_history[-1].action.params == {"x": 299, "y": 90}
            assert session.loop.context.action_history[-1].success
            assert session.loop.state == AgentState.VERIFYING and len(requests) == 2
        elif mode in {"thin-crop", "nested-thin-crop"}:
            assert after == (1277, 330)
            assert session.loop.context.action_history[-1].action.params == {"x": 1277, "y": 330}
            assert session.loop.context.action_history[-1].success
            assert session.loop.state == AgentState.VERIFYING
            assert len(requests) == (3 if mode == "nested-thin-crop" else 2)
        elif mode in histories:
            expected = (90, 70) if mode == "repeat-radius" else (200, 180)
            assert after == expected
            assert len(requests) == (2 if mode == "repeat-radius" else 1)
            assert session.loop.state == AgentState.VERIFYING
            assert session.loop.context.action_history[-1].success
            assert session.loop.context.action_history[-1].action.params == {
                "x": expected[0],
                "y": expected[1],
            }
        else:
            assert after == (40, 40)
            assert session.loop.context.action_history == previous
            assert session.loop.state == AgentState.RECOVERING
            await session._recover_phase()
            assert session.loop.state == AgentState.FAILED
            assert len(requests) == (4 if mode == "repeat-depth" else 3)
        for body in requests[1:2]:
            assert any(p.get("type") == "image_url" for p in body["messages"][-1]["content"])
    finally:
        (tmp_path / "model-requests.json").write_text(json.dumps(requests, ensure_ascii=False))
        await session.aclose()
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
