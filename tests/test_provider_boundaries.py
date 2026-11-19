"""Validate model wire responses before they become bounded desktop actions."""

import io
import json
from contextlib import nullcontext

import httpx
import pytest
from openai import BadRequestError
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits, TaskBudget, TaskLimitError
from ai_desktop_agent.agent.llm.base import ModelResponseError
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.llm.types import ErrorContext
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.privacy import unprotect
from ai_desktop_agent.vm.screenshot import Screenshot
from tests.test_task_budget import completion, llm_with_transport


def screenshot(size=(32, 24)):
    image = io.BytesIO()
    Image.new("RGB", size, "white").save(image, format="PNG")
    return Screenshot(image.getvalue(), *size)


@pytest.mark.parametrize("failure", ["invalid-image", "empty-image", "codec-failure"])
async def test_failed_image_encoding_does_not_send_a_mislabeled_image_or_charge_a_request(
    monkeypatch, failure
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return completion('{"action_type":"mouse_move","params":{"x":1,"y":1}}')

    frame = screenshot()
    if failure == "invalid-image":
        frame = Screenshot(b"invalid image", 32, 24)
    elif failure == "empty-image":
        frame = Screenshot(b"", 32, 24)
    else:
        save = Image.Image.save

        def unavailable(image, fp, format=None, **kwargs):
            if format == "JPEG":
                raise OSError("JPEG codec unavailable")
            return save(image, fp, format=format, **kwargs)

        monkeypatch.setattr(Image.Image, "save", unavailable)
    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(ValueError, match="画像"):
            await provider.decide_next_action(Goal("inspect"), Subtask("s1", "inspect"), [], frame)
        assert sent == [] and provider.call_count == 0
    finally:
        await provider.aclose()


async def test_sensitive_default_headers_are_sent_but_never_echoed_into_model_text():
    tokens = ["audit-bearer-value-20261118", "audit-api-key-value-20261118"]
    headers = {
        "Authorization": "Bearer " + tokens[0],
        "X-Api-Key": tokens[1],
        "X-Feature": "coordinate-check",
    }
    requests = []

    def handle(request):
        requests.append(request)
        return completion(
            json.dumps({"intent": " ".join(tokens), "constraints": [], "reasoning": "checked"})
        )

    provider = OpenAICompatProvider(api_key="local-header-test-key", default_headers=headers)
    replacement = provider._client.with_options(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    await provider._client.close()
    provider._client = replacement
    try:
        result = await provider.understand_instruction(Goal("inspect " + " ".join(tokens)))
        assert result.intent == "[REDACTED] [REDACTED]"
        assert len(requests) == provider.call_count == 1
        for key, value in headers.items():
            assert requests[0].headers[key] == value
        prompt = json.loads(requests[0].content)["messages"][-1]["content"]
        assert all(token not in prompt for token in tokens)
    finally:
        await provider.aclose()
        for token in [*tokens, headers["Authorization"]]:
            unprotect(token)


@pytest.mark.parametrize("budgeted", [False, True])
async def test_invalid_http_json_cannot_hide_request_usage_or_retry_without_a_bound(
    budgeted, provider_retry_sleep
):
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, content=b"{broken", headers={"content-type": "application/json"})

    provider = await llm_with_transport(handle)
    budget = TaskBudget(ExecutionLimits(max_seconds=5, request_timeout_seconds=2))
    manager = budget.activate(lambda: None) if budgeted else nullcontext()
    try:
        with pytest.raises(TaskLimitError if budgeted else ModelResponseError) as failed:
            async with manager:
                await provider.understand_instruction(Goal("inspect the saved document"))
        assert len(sent) == provider.call_count == (1 if budgeted else 3)
        assert [call.args[0] for call in provider_retry_sleep.await_args_list] == (
            [] if budgeted else [1.5, 3.0]
        )
        if budgeted:
            assert failed.value.reason == "usage_unavailable"
            assert budget.requests == 1 and not budget.usage_complete
        else:
            assert budget.requests == 0
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "kind,params,size,expected",
    [
        (
            "drag",
            {"start_x": -20, "start_y": 500, "end_x": 500, "end_y": -20},
            (32, 24),
            {"start_x": 0, "start_y": 23, "end_x": 31, "end_y": 0},
        ),
        (
            "region_select",
            {"x": -20, "y": 500, "width": 1, "height": 500},
            (32, 24),
            {"x": 0, "y": 23, "width": 16, "height": 24},
        ),
        (
            "region_select",
            {"x": 0, "y": 0, "width": 1, "height": 1},
            (8, 12),
            {"x": 0, "y": 0, "width": 8, "height": 12},
        ),
        ("left_click", {"x": 500, "y": -20}, (32, 24), {"x": 31, "y": 0}),
        ("subtask_complete", {}, (32, 24), {}),
    ],
)
async def test_model_coordinates_are_normalized_only_after_action_validation(
    kind, params, size, expected
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return completion(json.dumps({"action_type": kind, "params": params, "confidence": 1}))

    provider = await llm_with_transport(handle)
    try:
        result = await provider.decide_next_action(
            Goal("inspect"), Subtask("s1", "inspect"), [], screenshot(size)
        )
        assert result.action.params == expected
        assert len(sent) == provider.call_count == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize("params", [None, [], "coordinates", 12])
async def test_nonobject_model_parameters_are_rejected_at_the_wire_boundary(params):
    provider = await llm_with_transport(
        lambda request: completion(json.dumps({"action_type": "left_click", "params": params}))
    )
    try:
        with pytest.raises(ValueError, match="params"):
            await provider.decide_next_action(
                Goal("inspect"), Subtask("s1", "inspect"), [], screenshot()
            )
        assert provider.call_count == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize("actions", [None, {}, "retry"])
async def test_nonarray_recovery_actions_cannot_produce_a_partial_plan(actions):
    provider = await llm_with_transport(
        lambda request: completion(
            json.dumps(
                {"strategy": "alternative_approach", "actions": actions, "recoverable": True}
            )
        )
    )
    try:
        with pytest.raises(ValueError, match="配列"):
            await provider.recover_from_error(
                ErrorContext(Action(ActionType.WAIT, {"seconds": 0}), "not ready", 0),
                [],
                Subtask("s1", "inspect"),
            )
        assert provider.call_count == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "mode",
    ["server-refusal", "long-cooldown", "last-attempt", "nonstring-message", "delayed-fallback"],
)
async def test_schema_rejection_respects_retry_directives_and_preserves_request_budget(
    mode, provider_retry_sleep
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        if mode == "last-attempt" and len(sent) < 3:
            return completion("invalid JSON")
        if mode == "delayed-fallback" and len(sent) == 2:
            return completion('{"intent":"inspect","constraints":[],"reasoning":"inspect"}')
        headers = {
            "server-refusal": {"x-should-retry": "false"},
            "long-cooldown": {"retry-after": "999"},
            "delayed-fallback": {"retry-after": "0.25"},
        }.get(mode, {})
        error = {
            "message": "response_format json_schema is not supported",
            "param": "response_format",
        }
        if mode == "nonstring-message":
            error["message"] = {"message": "unsupported"}
        return httpx.Response(400, json={"error": error}, headers=headers)

    provider = await llm_with_transport(handle)
    try:
        if mode == "delayed-fallback":
            assert (await provider.understand_instruction(Goal("inspect"))).intent == "inspect"
            assert len(sent) == 2 and "response_format" not in sent[-1]
            assert [call.args[0] for call in provider_retry_sleep.await_args_list] == [0.25]
        else:
            with pytest.raises(BadRequestError) as failed:
                await provider.understand_instruction(Goal("inspect"))
            assert getattr(failed.value, "status_code", None) == 400
            assert len(sent) == (3 if mode == "last-attempt" else 1)
            assert [call.args[0] for call in provider_retry_sleep.await_args_list] == (
                [1.5, 3.0] if mode == "last-attempt" else []
            )
        assert provider.call_count == len(sent)
    finally:
        await provider.aclose()
