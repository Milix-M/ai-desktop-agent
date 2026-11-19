"""A model's metadata is untrusted, including when the JSON syntax is valid."""

import copy
import json

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient
from tests.test_task_budget import completion, llm_with_transport

# The keys name the wire stage and the invalid metadata, not an implementation helper.
CORRUPTIONS = {
    "understand.intent": {"unexpected": "object"},
    "understand.target_application": ["terminal"],
    "understand.constraints": "do not type",
    "understand.constraints-item": [False],
    "understand.reasoning": ["inspect"],
    "plan.subtasks": {},
    "plan.subtasks-null": None,
    "plan.subtasks-item": [None],
    "plan.id": None,
    "plan.id-empty": "",
    "plan.id-blank": "  ",
    "plan.id-list": ["s1"],
    "plan.id-duplicate": "s1",
    "plan.description": ["inspect"],
    "plan.expected_outcome": 1,
    "plan.reasoning": {"inspect": True},
    "action.expected_effect": None,
    "action.reasoning": ["inspect"],
    "action.reasoning-unicode": "unpaired \ud800",
    "action.text-unicode": "unpaired \ud800",
    "action.keys-unicode": ["ctrl", "unpaired \ud800"],
    "verify.reasoning": ["visible"],
    "verify.evidence": {"visible": True},
    "recover.reasoning": ["try again"],
    "recover.reasoning-unicode": "unpaired \udfff",
}


def metadata_reply(stage, case, text="forbidden input", *, action_number=1):
    """A complete local model flow; an accepted bad response can really request input."""
    step = {"id": "s1", "description": "inspect the report", "expected_outcome": "report visible"}
    values = {
        "understand_instruction": {
            "intent": "inspect",
            "target_application": None,
            "constraints": [],
            "reasoning": "inspect",
        },
        "decompose_task": {"subtasks": [step], "reasoning": "inspect"},
        "action_decision": {
            "action_type": "subtask_complete",
            "params": {},
            "confidence": 1,
            "reasoning": "report visible",
            "expected_effect": "report visible",
        },
        "verify_result": {"success": True, "reasoning": "visible", "evidence": "report visible"},
        "recover_from_error": {
            "strategy": "alternative_approach",
            "actions": [{"action_type": "type", "params": {"text": text}}],
            "recoverable": True,
            "reasoning": "inspect again",
        },
    }
    group, field = case.split(".")
    if group in {"understand", "plan", "action"} and action_number == 1:
        values["action_decision"].update(action_type="type", params={"text": text})
    if group == "recover":
        values["verify_result"]["success"] = False
    value = copy.deepcopy(values[stage])
    target_stage = {
        "understand": "understand_instruction",
        "plan": "decompose_task",
        "action": "action_decision",
        "verify": "verify_result",
        "recover": "recover_from_error",
    }[group]
    if stage == target_stage and case in CORRUPTIONS:
        bad = CORRUPTIONS[case]
        if group == "action" and field == "text-unicode":
            value["params"]["text"] = bad
        elif group == "action" and field == "keys-unicode":
            value.update(action_type="key_combo", params={"keys": bad})
        elif group == "plan" and field == "id-duplicate":
            value["subtasks"].append({**step, "description": "inspect another report"})
        elif group == "plan" and field.startswith(("id", "description", "expected_outcome")):
            value["subtasks"][0][field.split("-")[0]] = bad
        else:
            value[field.split("-")[0]] = bad
    return value


@pytest.mark.parametrize("case", CORRUPTIONS)
async def test_invalid_metadata_stops_before_input_or_completion_and_keeps_readable_history(
    tmp_path, monkeypatch, case, session_retry_sleep
):
    requests = []
    actions = 0

    def respond(request):
        nonlocal actions
        stage = json.loads(request.content)["response_format"]["json_schema"]["name"]
        requests.append(stage)
        if stage == "action_decision":
            actions += 1
        return completion(json.dumps(metadata_reply(stage, case, action_number=actions or 1)))

    provider = await llm_with_transport(respond)
    display = FakeDisplayBackend()
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=provider,
        display=display,
        store=store,
        decision=None,
        limits=ExecutionLimits(max_requests=25, max_seconds=10),
    )
    assert await session.run("inspect the visible report without entering text") is False
    assert display.text_inputs == [] and display.key_combos == [] and not display.is_connected
    record = TaskStore(tmp_path).load(session.id)
    assert record is not None and record.state == "failed" and record.success is False
    assert record.stop_reason == "execution_error" and record.error_message
    group, field = case.split(".")
    assert field.split("-")[0] in record.error_message
    assert all(item.action_type == "subtask_complete" for item in record.actions)
    if group in {"understand", "plan", "action"}:
        assert record.actions == [] and record.current_subtask_index == 0
    else:
        assert record.current_subtask_index == 0
        assert requests.count("verify_result") == 1
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.get(f"/tasks/{session.id}")
    assert response.status_code == 200
    assert response.json()["error_message"] == record.error_message


@pytest.mark.parametrize("omit_optional_metadata", [False, True])
async def test_valid_unicode_and_legacy_defaults_remain_usable(
    tmp_path, omit_optional_metadata, session_retry_sleep
):
    text = "売上報告 👩‍💻 café\n"
    actions = 0

    def respond(request):
        nonlocal actions
        stage = json.loads(request.content)["response_format"]["json_schema"]["name"]
        if stage == "action_decision":
            actions += 1
        data = metadata_reply(stage, "action.valid", text, action_number=actions or 1)
        if omit_optional_metadata:
            for field in (
                "intent",
                "target_application",
                "constraints",
                "reasoning",
                "evidence",
                "expected_effect",
                "confidence",
            ):
                data.pop(field, None)
            if stage == "decompose_task":
                data["subtasks"] = [{"description": "inspect"}]
        return completion(json.dumps(data))

    provider = await llm_with_transport(respond)
    display = FakeDisplayBackend()
    session = TaskSession(llm=provider, display=display, store=TaskStore(tmp_path), decision=None)
    assert await session.run("売上報告を入力する") is True
    saved = TaskStore(tmp_path).load(session.id)
    assert display.text_inputs == [text] and not display.is_connected
    assert saved.state == "completed" and saved.success is True and saved.stop_reason is None
    assert [item.action_type for item in saved.actions] == ["type", "subtask_complete"]
    assert saved.actions[0].params["text"] == text


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: Action(ActionType.SUBTASK_COMPLETE, description={"invalid": True}),
            id="action-description-object",
        ),
        pytest.param(
            lambda: Action(ActionType.SUBTASK_COMPLETE, description="invalid \ud800"),
            id="action-description-unicode",
        ),
        pytest.param(
            lambda: UnderstandingResult("inspect", None, ("no typing",), ""), id="constraints-tuple"
        ),
        pytest.param(
            lambda: DecompositionResult((Subtask("s1", "inspect"),), ""), id="subtasks-tuple"
        ),
        pytest.param(lambda: DecompositionResult([{}], ""), id="untyped-subtask"),
        pytest.param(lambda: ActionDecision({}, "", 1, ""), id="untyped-action"),
        pytest.param(
            lambda: ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "", 1, "", 1),
            id="zoom-flag",
        ),
        pytest.param(lambda: VerificationResult("false", "", ""), id="verification-flag"),
        pytest.param(lambda: RecoveryPlan("give_up", [], "", False), id="untyped-strategy"),
        pytest.param(
            lambda: RecoveryPlan(RecoveryStrategy.GIVE_UP, (), "", False), id="actions-tuple"
        ),
        pytest.param(
            lambda: RecoveryPlan(RecoveryStrategy.GIVE_UP, [{}], "", False),
            id="untyped-recovery-action",
        ),
        pytest.param(
            lambda: RecoveryPlan(RecoveryStrategy.GIVE_UP, [], "", "false"), id="recovery-flag"
        ),
    ],
)
def test_an_alternate_provider_cannot_construct_unsafe_response_objects(factory):
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_retries", True),
        ("max_retries", 0.5),
        ("max_retries", float("nan")),
        ("timeout_seconds", False),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", 10**1000),
        ("description", "unpaired \ud800"),
        ("expected_outcome", None),
    ],
    ids=[
        "boolean-retries",
        "fractional-retries",
        "nan-retries",
        "boolean-timeout",
        "infinite-timeout",
        "nan-timeout",
        "huge-timeout",
        "invalid-unicode",
        "null-outcome",
    ],
)
def test_subtask_metadata_cannot_bypass_its_bounds_or_break_history(field, value):
    with pytest.raises(ValueError, match=field):
        Subtask(**({"id": "s1", "description": "inspect"} | {field: value}))
