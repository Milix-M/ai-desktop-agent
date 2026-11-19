"""Visual click failures must reach recovery and its retry budget."""

import io
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ErrorContext, RecoveryPlan, RecoveryStrategy
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend

CLICK_TYPES = [
    ActionType.LEFT_CLICK,
    ActionType.RIGHT_CLICK,
    ActionType.DOUBLE_CLICK,
    ActionType.MIDDLE_CLICK,
]


@pytest.fixture
async def session():
    llm = MockLLMProvider()
    llm.recover_from_error = AsyncMock(
        return_value=RecoveryPlan(
            RecoveryStrategy.ALTERNATIVE_APPROACH,
            [Action(ActionType.KEY_PRESS, {"key": "escape"})],
            "ダイアログを閉じて別の操作を試す",
            True,
        )
    )
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=None)
    session.loop.start(Goal(description="報告書を保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask(id="save", description="保存先を選ぶ")])
    try:
        yield session
    finally:
        await session.aclose()


async def click_to_verify(session, kind):
    session._last_before_raw = session.display.capture_raw()
    action = Action(kind, {"x": 20, "y": 30})
    assert await session._execute_recorded_action(action, "保存先を開く")
    session.loop.action_executed()
    session.loop.wait_complete()
    return action


@pytest.mark.parametrize("kind", CLICK_TYPES)
async def test_unchanged_click_is_explained_to_recovery_without_falsifying_transport_success(
    session, kind
):
    action = await click_to_verify(session, kind)
    await session._verify_phase()
    assert session.loop.state == AgentState.RECOVERING
    assert session.loop.context.action_history[-1].success is True
    assert session._pending_error is not None
    assert session._pending_error.action == action
    assert "画面変化" in session._pending_error.error_message
    await session._recover_phase()
    session.llm.recover_from_error.assert_awaited_once()
    error = session.llm.recover_from_error.call_args.args[0]
    assert error.action == action and "画面変化" in error.error_message
    assert session.display.key_presses == ["escape"]
    assert session.loop.context.retry_counts == {f"save:{kind.value}": 1}
    assert session.loop.state == AgentState.EXECUTING


async def test_repeated_visual_failures_exhaust_the_subtask_retry_budget(session):
    session.loop.context.current_subtask.max_retries = 1
    for _ in range(2):
        await click_to_verify(session, ActionType.LEFT_CLICK)
        await session._verify_phase()
        await session._recover_phase()
    assert session.loop.state == AgentState.FAILED
    assert session.llm.recover_from_error.await_count == 1
    assert session.display.key_presses == ["escape"]
    assert session.loop.context.retry_counts == {"save:left_click": 1}


@pytest.mark.parametrize("kind", CLICK_TYPES)
async def test_visible_click_changes_continue_without_recovery(session, kind):
    await click_to_verify(session, kind)
    pixels = io.BytesIO()
    Image.new("RGB", (1024, 768), color=(80, 160, 220)).save(pixels, format="PNG")
    session.display.capture_raw = Mock(
        return_value=replace(
            session._last_before_raw,
            image_bytes=pixels.getvalue(),
        )
    )
    await session._verify_phase()
    assert session.loop.state == AgentState.EXECUTING
    assert session._pending_error is None
    session.llm.recover_from_error.assert_not_awaited()


async def test_failed_effect_capture_reaches_recovery_with_the_current_action(session):
    action = await click_to_verify(session, ActionType.RIGHT_CLICK)
    session.display.capture_raw = Mock(side_effect=OSError("capture unavailable"))
    await session._verify_phase()
    assert session.loop.state == AgentState.RECOVERING
    assert session._pending_error.action == action
    await session._recover_phase()
    session.llm.recover_from_error.assert_awaited_once()
    assert session.llm.recover_from_error.call_args.args[0].action == action


async def test_current_visual_failure_replaces_an_old_recovery_error(session):
    action = await click_to_verify(session, ActionType.LEFT_CLICK)
    session._pending_error = ErrorContext(
        Action(ActionType.KEY_PRESS, {"key": "escape"}), "古い失敗", 2
    )
    await session._verify_phase()
    assert session._pending_error.action == action
    assert "古い失敗" not in session._pending_error.error_message
    await session._recover_phase()
    assert session.loop.context.retry_counts == {"save:left_click": 1}


async def test_backend_without_raw_capture_can_still_detect_unchanged_clicks(session):
    await click_to_verify(session, ActionType.LEFT_CLICK)
    session.display.capture_raw = None
    await session._verify_phase()
    assert session.loop.state == AgentState.RECOVERING
    assert session.display.screenshots_taken == 2
    await session._recover_phase()
    session.llm.recover_from_error.assert_awaited_once()


async def test_a_missing_comparison_frame_keeps_legacy_behavior_without_inventing_failure(session):
    await click_to_verify(session, ActionType.LEFT_CLICK)
    session._last_before_raw = None
    await session._verify_phase()
    assert session.loop.state == AgentState.EXECUTING
    assert session._pending_error is None


async def test_a_mechanical_click_failure_preserves_its_error_and_skips_visual_capture(session):
    session.display.mouse_click = Mock(side_effect=OSError("click transport failed"))
    action = Action(ActionType.LEFT_CLICK, {"x": 20, "y": 30})
    assert not await session._execute_recorded_action(action, "保存先を開く")
    session.loop.action_executed()
    session.loop.wait_complete()
    session.display.capture_raw = Mock(
        side_effect=AssertionError("must not capture after failed click")
    )
    await session._verify_phase()
    assert session.loop.state == AgentState.RECOVERING
    assert session._pending_error.error_message == "click transport failed"
    assert not session.loop.context.action_history[-1].success
    session.display.capture_raw.assert_not_called()


async def test_verification_without_an_action_does_not_capture_or_request_recovery(session):
    session.loop.action_executed()
    session.loop.wait_complete()
    session.display.capture_raw = Mock(side_effect=AssertionError("there was no click"))
    await session._verify_phase()
    assert session.loop.state == AgentState.EXECUTING
    session.display.capture_raw.assert_not_called()
    session.llm.recover_from_error.assert_not_awaited()
