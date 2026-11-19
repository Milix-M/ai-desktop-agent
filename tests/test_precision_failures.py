"""Precision decisions retain permission, confidence, bounds and recovery limits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, RecoveryPlan, RecoveryStrategy
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server import session as session_module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def decision(kind, confidence=1.0, **params):
    return ActionDecision(Action(kind, params), "select the visible target", confidence, "target")


def region():
    return decision(ActionType.REGION_SELECT, x=100, y=80, width=200, height=180)


def executing(entry, replies):
    initial = (
        region()
        if entry == "region"
        else decision(
            ActionType.LEFT_CLICK,
            confidence=0.2 if entry == "low-confidence" else 1,
            x=200,
            y=180,
        )
    )
    model = MockLLMProvider(
        recover_result=RecoveryPlan(RecoveryStrategy.GIVE_UP, [], "cannot locate target", False)
    )
    model.decide_next_action = AsyncMock(side_effect=[initial, *replies])
    model.recover_from_error = AsyncMock(wraps=model.recover_from_error)
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    session = TaskSession(llm=model, display=display, decision=None)
    session.loop.start(Goal("select the visible target"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("s1", "select the target")])
    if entry == "repeat":
        session.loop.record_action(initial.action, True)
    return session, initial


@pytest.mark.parametrize("entry", ["region", "low-confidence", "repeat"])
@pytest.mark.parametrize("failure", ["capture", "depth", "low-confidence"])
async def test_failed_precision_never_falls_back_to_an_unsafe_click_and_explains_recovery(
    entry, failure
):
    replies = (
        [region(), region()]
        if failure == "depth"
        else [decision(ActionType.LEFT_CLICK, confidence=0.2, x=50, y=60)]
    )
    session, initial = executing(entry, replies)
    if failure == "capture":
        session.display.capture_raw = Mock(side_effect=OSError("raw capture lost"))
    previous = list(session.loop.context.action_history)
    errors = []
    session.on_error(errors.append)
    try:
        await session._execute_phase()
        assert session.display.clicks == []
        assert session.loop.context.action_history == previous
        assert session.loop.state == AgentState.RECOVERING
        assert session._pending_error is not None
        assert session._pending_error.action == initial.action
        assert errors == [session._pending_error.error_message]
        await session._recover_phase()
        assert session.loop.state == AgentState.FAILED
        assert session.llm.recover_from_error.await_count == 1
        assert session.llm.recover_from_error.call_args.args[0].action == initial.action
    finally:
        await session.aclose()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("entry", ["region", "low-confidence", "repeat"])
async def test_restart_chosen_after_zoom_uses_the_same_permission_and_audit_path(
    monkeypatch, entry, allowed
):
    restart = decision(ActionType.VM_RESTART, reason="application is unresponsive")
    session, _ = executing(entry, [restart])
    session.allow_vm_restart = allowed
    client = Mock()
    monkeypatch.setattr("docker.from_env", Mock(return_value=client))
    monkeypatch.setattr(session_module, "RESTART_SETTLE_SECONDS", 0)
    perform = Mock()

    def restart_vm(pool, vm_id):
        assert pool._docker() is client
        client.close.assert_not_called()
        perform(vm_id)
        return SimpleNamespace(id="desktop")

    monkeypatch.setattr("ai_desktop_agent.server.vm_pool.VmPool.restart_vm", restart_vm)
    try:
        await session._execute_phase()
        assert session.loop.state == AgentState.RECOVERING
        record = session.loop.context.action_history[-1]
        assert record.action == restart.action and record.success is allowed
        assert record.reasoning == restart.reasoning
        assert "再起動" in session._pending_error.error_message
        assert perform.call_count == int(allowed)
        assert client.close.call_count == int(allowed)
    finally:
        await session.aclose()


@pytest.mark.parametrize("relative", [-10, 400, 10**1000], ids=["negative", "past-crop", "huge"])
async def test_zoomed_coordinates_are_clamped_to_the_visible_crop_before_inverse_mapping(relative):
    session, _ = executing("region", [decision(ActionType.LEFT_CLICK, x=relative, y=20)])
    session.executor.wait_for_still = AsyncMock()
    try:
        await session._execute_phase()
        assert session.display.clicks == [{"button": 1, "x": 100 if relative < 0 else 299, "y": 90}]
    finally:
        await session.aclose()


@pytest.mark.parametrize("entry", ["region", "low-confidence", "repeat"])
async def test_repeated_precision_failure_exhausts_recovery_without_spending_gui_actions(entry):
    session, initial = executing(entry, [])

    async def decide(*args, **kwargs):
        return (
            decision(ActionType.LEFT_CLICK, confidence=0.2, x=20, y=30)
            if kwargs.get("is_zoomed")
            else initial
        )

    session.llm.decide_next_action = AsyncMock(side_effect=decide)
    session.llm.recover_from_error = AsyncMock(
        return_value=RecoveryPlan(
            RecoveryStrategy.ALTERNATIVE_APPROACH, [], "try a different location", True
        )
    )
    session.loop.context.current_subtask.max_retries = 1
    previous = list(session.loop.context.action_history)
    try:
        for _ in range(2):
            await session._execute_phase()
            await session._recover_phase()
        assert session.loop.state == AgentState.FAILED
        assert session.display.clicks == [] and session.loop.context.action_history == previous
        assert session.llm.decide_next_action.await_count == 4
        assert session.llm.recover_from_error.await_count == 1
        assert session.loop.context.retry_counts == {f"s1:{initial.action.action_type.value}": 1}
    finally:
        await session.aclose()


async def test_recovery_can_complete_after_a_rejected_precision_guess(
    tmp_path, session_retry_sleep
):
    plan = RecoveryPlan(
        RecoveryStrategy.ALTERNATIVE_APPROACH,
        [Action(ActionType.MOUSE_MOVE, {"x": 20, "y": 30})],
        "inspect another visible target",
        True,
    )
    model = MockLLMProvider(recover_result=plan)
    model.decide_next_action = AsyncMock(
        side_effect=[
            region(),
            decision(ActionType.LEFT_CLICK, confidence=0.2, x=20, y=30),
            decision(ActionType.SUBTASK_COMPLETE),
        ]
    )
    display = FakeDisplayBackend()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    assert await session.run("inspect the visible target") is True
    assert display.clicks == [] and display.mouse_moves == [(20, 30)]
    error = model.decide_next_action.call_args.kwargs["error_context"]
    assert "操作位置" in error.error_message and plan.reasoning in error.error_message
    record = store.load(session.id)
    assert record.state == "completed" and record.success is True
    assert [a.action_type for a in record.actions] == ["mouse_move", "subtask_complete"]


async def test_huge_region_request_is_bounded_before_float_conversion_or_image_allocation():
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    model = MockLLMProvider(decide_result=decision(ActionType.LEFT_CLICK, x=0, y=0))
    session = TaskSession(llm=model, display=display, decision=None)
    try:
        result = await session._handle_region_zoom(
            Subtask("s", "target"),
            decision(
                ActionType.REGION_SELECT,
                x=10**1000,
                y=10**1000,
                width=10**1000,
                height=10**1000,
            ),
        )
        assert result.action.params == {"x": 639, "y": 479}
        assert model.decide_call_count == 1
    finally:
        await session.aclose()


async def test_zoom_drag_maps_both_endpoints_inside_the_visible_crop():
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    model = MockLLMProvider(
        decide_result=decision(
            ActionType.DRAG,
            start_x=-10,
            start_y=50000,
            end_x=50000,
            end_y=-10,
        )
    )
    session = TaskSession(llm=model, display=display, decision=None)
    try:
        result = await session._handle_region_zoom(Subtask("s", "target"), region())
        assert await session.executor.execute(result.action)
        assert display.drags == [{"start": (100, 259), "end": (299, 80), "button": 1}]
    finally:
        await session.aclose()


async def test_pausing_during_a_zoom_response_prevents_another_model_call_until_resume():
    session, _ = executing("region", [])
    first_returned = asyncio.Event()
    calls = []

    async def decide(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return region()
        if len(calls) == 2:
            session.pause()
            first_returned.set()
            return region()
        return decision(ActionType.LEFT_CLICK, x=20, y=30)

    session.llm.decide_next_action = decide
    session.executor.wait_for_still = AsyncMock()
    phase = asyncio.create_task(session._execute_phase())
    try:
        await asyncio.wait_for(first_returned.wait(), 1)
        await asyncio.sleep(0.05)
        assert len(calls) == 2 and not phase.done()
        assert session.display.clicks == []
        session.resume()
        await asyncio.wait_for(phase, 2)
        assert len(calls) == 3 and len(session.display.clicks) == 1
    finally:
        phase.cancel()
        await asyncio.gather(phase, return_exceptions=True)
        await session.aclose()


@pytest.mark.parametrize(
    "kind,params",
    [
        (ActionType.REGION_SELECT, {"x": 10, "y": 20, "width": 100, "height": 100}),
        (ActionType.VM_RESTART, {"reason": "unresponsive"}),
    ],
)
async def test_session_only_actions_cannot_be_reported_as_success_by_the_display_executor(
    kind, params
):
    display = FakeDisplayBackend()
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    try:
        assert await session.executor.execute(Action(kind, params)) is False
        assert session.executor.last_error
        assert display.clicks == []
    finally:
        await session.aclose()
