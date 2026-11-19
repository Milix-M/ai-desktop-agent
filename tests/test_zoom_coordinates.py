"""Validate model coordinates against real cropped images and dispatched input."""

import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot


def decision(kind, **params):
    return ActionDecision(Action(kind, params), "target selected", 1.0, "visible target")


@pytest.mark.parametrize("nested", [False, True])
async def test_zoom_click_reaches_the_target_in_the_original_image(nested):
    image = Image.new("RGB", (640, 480), "white")
    image.putpixel((170, 150), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = Screenshot(buffer.getvalue(), 640, 480)
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    display.capture_raw = lambda: raw
    llm = MockLLMProvider()
    replies = []
    if nested:
        replies.append(decision(ActionType.REGION_SELECT, x=40, y=60, width=160, height=140))
    replies.append(
        decision(ActionType.LEFT_CLICK, x=100 if nested else 140, y=80 if nested else 140)
    )
    llm.decide_next_action = AsyncMock(side_effect=replies)
    session = TaskSession(llm=llm, display=display)
    try:
        subtask = Subtask("s1", "select the red target")
        session.loop.start(Goal("select the red target"))
        session.loop.understanding_done()
        session.loop.plan_ready([subtask])
        result = await session._handle_region_zoom(
            subtask, decision(ActionType.REGION_SELECT, x=100, y=80, width=200, height=180)
        )
        assert result is not None
        assert await session.executor.execute(result.action)
        assert display.clicks == [{"button": 1, "x": 170, "y": 150}]
        calls = llm.decide_next_action.call_args_list
        assert calls[0].kwargs["screenshot"].width == 400
        if nested:
            assert calls[1].kwargs["screenshot"].width == 160
            assert calls[1].kwargs["zoom_origin"] == (120, 110)
            assert calls[1].kwargs["zoom_scale"] == 2.0
    finally:
        await session.aclose()


async def test_zoom_depth_limit_does_not_dispatch_an_unresolved_region():
    display = FakeDisplayBackend()
    llm = MockLLMProvider()
    region = decision(ActionType.REGION_SELECT, x=0, y=0, width=100, height=100)
    llm.decide_next_action = AsyncMock(return_value=region)
    session = TaskSession(llm=llm, display=display)
    try:
        assert await session._handle_region_zoom(Subtask("s1", "target"), region) is None
        assert llm.decide_next_action.await_count == 2
        assert not display.clicks
    finally:
        await session.aclose()
