"""Malformed model output must never become a click or a successful verification."""

from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.decision.provider import _extract_choice, _extract_prob
from ai_desktop_agent.agent.decision.routing import route_recovery, route_verify
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, ErrorContext
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot


def provider_with(response):
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider._call = AsyncMock(return_value=response)
    return provider


@pytest.mark.parametrize("success", [None, "false", "true", 0, 1, [], {}])
async def test_verification_requires_a_boolean(success):
    provider = provider_with({"success": success})
    decision = ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "", 1.0, "")
    with pytest.raises(ValueError):
        await provider.verify_result(decision, "report saved")


async def test_verification_cannot_default_to_success():
    decision = ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "", 1.0, "")
    with pytest.raises(ValueError):
        await provider_with({}).verify_result(decision, "report saved")


@pytest.mark.parametrize("action_type", [None, "click", "", [], {}])
async def test_unknown_actions_cannot_become_completion(action_type):
    provider = provider_with({"action_type": action_type, "params": {}})
    with pytest.raises(ValueError):
        await provider.decide_next_action(
            Goal("save report"), Subtask("s1", "save"), [], Screenshot(b"", 1024, 768)
        )


@pytest.mark.parametrize("coordinate", [None, True, "left", "100", float("nan"), float("inf")])
async def test_invalid_coordinate_cannot_become_top_left_click(coordinate):
    provider = provider_with({"action_type": "left_click", "params": {"x": coordinate, "y": 10}})
    with pytest.raises(ValueError):
        await provider.decide_next_action(
            Goal("save report"), Subtask("s1", "save"), [], Screenshot(b"", 1024, 768)
        )


@pytest.mark.parametrize(
    ("action_type", "params"),
    [
        (ActionType.WAIT, {"seconds": float("nan")}),
        (ActionType.WAIT, {"seconds": -1}),
        (ActionType.WAIT_FOR_STILL, {"timeout": float("inf")}),
        (ActionType.KEY_HOLD, {"key": "shift", "duration_ms": -1}),
        (ActionType.SCROLL, {"direction": "sideways", "amount": 3}),
        (ActionType.SCROLL, {"direction": "down", "amount": True}),
        (ActionType.SCROLL, {"direction": "down", "amount": -3}),
        (ActionType.TYPE, {"text": ["hello"]}),
        (ActionType.KEY_COMBO, {"keys": "ctrl+c"}),
        (ActionType.KEY_COMBO, {"keys": []}),
        (ActionType.KEY_PRESS, {"key": ""}),
        (ActionType.REGION_SELECT, {"x": 0, "y": 0, "width": 0, "height": 20}),
    ],
)
def test_action_values_are_validated_before_execution(action_type, params):
    with pytest.raises(ValueError):
        Action(action_type, params)


@pytest.mark.parametrize(
    "bad_action", [{"action_type": "click"}, {"action_type": "left_click", "params": {}}, None]
)
async def test_recovery_does_not_execute_the_tail_of_an_invalid_plan(bad_action):
    provider = provider_with(
        {
            "strategy": "alternative_approach",
            "recoverable": True,
            "actions": [bad_action, {"action_type": "type", "params": {"text": "report"}}],
        }
    )
    with pytest.raises(ValueError):
        await provider.recover_from_error(
            ErrorContext(Action(ActionType.SUBTASK_COMPLETE), "not saved", 1),
            [],
            Subtask("s1", "save"),
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_probabilities_do_not_authorize_completion_or_recovery(value):
    with pytest.raises(ValueError):
        _extract_prob({"verify_achieved": {"prob": value}}, ("verify_achieved",))
    with pytest.raises(ValueError):
        _extract_choice({"recover_strategy": {"choice": "wait_and_retry", "prob": value}})
    assert route_verify(value, "full", high=0.9, low=0.6) == "escalate"
    assert route_recovery(value, minimum=0.6) is False


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_json_rejects_nonfinite_numbers(literal):
    with pytest.raises(ValueError):
        OpenAICompatProvider._parse_json('{"confidence": ' + literal + "}")


def test_truncated_code_fence_can_recover_a_complete_json_object():
    assert OpenAICompatProvider._parse_json('```json\n{"success": false}') == {"success": False}


@pytest.mark.parametrize("value", [None, True, "0.9", float("nan"), float("inf"), -1, 2])
async def test_action_confidence_is_a_probability(value):
    provider = provider_with({"action_type": "subtask_complete", "confidence": value})
    with pytest.raises(ValueError):
        await provider.decide_next_action(
            Goal("save report"), Subtask("s1", "save"), [], Screenshot(b"", 1024, 768)
        )


@pytest.mark.parametrize("choice", ["wait_and_retry", {"choice": "wait_and_retry"}])
def test_missing_probability_cannot_invent_recovery_confidence(choice):
    with pytest.raises(ValueError):
        _extract_choice({"recover_strategy": choice})


@pytest.mark.parametrize("value", [None, "false", 0, 1])
async def test_recoverability_requires_a_boolean(value):
    provider = provider_with({"strategy": "wait_and_retry", "actions": [], "recoverable": value})
    with pytest.raises(ValueError):
        await provider.recover_from_error(
            ErrorContext(Action(ActionType.SUBTASK_COMPLETE), "not saved", 1),
            [],
            Subtask("s1", "save"),
        )


@pytest.mark.parametrize(
    "response",
    [
        {"action_type": "left_click", "params": {"x": "save button", "y": 10}},
        {"action_type": "unsupported_action"},
    ],
)
async def test_bad_decision_fails_and_persists_without_any_desktop_input(tmp_path, response):
    from ai_desktop_agent.agent.llm.mock import MockLLMProvider
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.server.store import TaskStore
    from ai_desktop_agent.vm.fake import FakeDisplayBackend

    llm = MockLLMProvider()
    llm.decide_next_action = provider_with(response).decide_next_action
    display = FakeDisplayBackend()
    image = BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, format="PNG")
    # A visible desktop avoids the independent black-screen wake-up Shift key.
    display.capture_screen = lambda **kwargs: Screenshot(image.getvalue(), 32, 32)
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    completed = []
    session.on_complete(completed.append)
    assert await session.run("save report") is False
    assert completed == [False]
    assert not display.clicks and not display.text_inputs and not display.key_presses
    assert not display.is_connected
    record = store.load(session.id)
    assert record.state == "failed" and record.success is False
    assert record.actions == []


async def test_string_false_cannot_advance_the_sessions_subtask():
    from ai_desktop_agent.agent.llm.mock import MockLLMProvider
    from ai_desktop_agent.agent.state import AgentState
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.vm.fake import FakeDisplayBackend

    llm = MockLLMProvider()
    llm.verify_result = provider_with({"success": "false"}).verify_result
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=None)
    session.loop.start(Goal("save report"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("s1", "save")])
    session.loop.record_action(Action(ActionType.SUBTASK_COMPLETE), True)
    session.loop.action_executed()
    session.loop.wait_complete()
    try:
        await session._verify_phase()
        assert session.loop.state == AgentState.RECOVERING
        assert session.loop.context.current_subtask_index == 0
    finally:
        await session.aclose()
