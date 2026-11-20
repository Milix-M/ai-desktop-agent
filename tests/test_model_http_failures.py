"""Model HTTP failures must preserve capabilities, retry bounds and usage records."""

import json

import httpx
import pytest
from openai import APIStatusError

from ai_desktop_agent.agent.budget import ExecutionLimits, TaskBudget, TaskLimitError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_task_budget import completion, llm_with_transport

SCHEMA = {"name": "report_status", "schema": {"type": "object"}}


@pytest.mark.parametrize(
    "status,message,param,code",
    [
        (
            400,
            "max_tokens is invalid; response_format json_schema was requested",
            "max_tokens",
            "invalid_value",
        ),
        (
            400,
            "Invalid schema for response_format",
            "response_format.json_schema",
            "invalid_json_schema",
        ),
        (401, "response_format requires a valid API key", "response_format", "invalid_api_key"),
        (
            403,
            "json_schema is not supported by this account's permissions",
            None,
            "permission_denied",
        ),
        (404, "Model json_schema_preview does not exist", "model", "model_not_found"),
        (413, "Input image is too large", "messages", "request_too_large"),
        (
            422,
            "Unsupported image in messages; response_format=json_schema",
            "messages",
            "invalid_image",
        ),
    ],
)
async def test_permanent_errors_are_not_retried_or_misclassified_as_schema_support(
    status, message, param, code, provider_retry_sleep
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            status, json={"error": {"message": message, "param": param, "code": code}}
        )

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError) as error:
            await provider._call("報告を確認する", SCHEMA)
        assert error.value.status_code == status
        assert len(sent) == provider.call_count == 1
        assert provider._structured_output is True
        assert "response_format" in sent[0]
        provider_retry_sleep.assert_not_awaited()
    finally:
        await provider.aclose()


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
async def test_transient_errors_retry_without_disabling_schema_support(
    status, provider_retry_sleep
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "json_schema temporarily unsupported: busy",
                    "code": "server_busy",
                }
            },
        )

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError):
            await provider._call("報告を確認する", SCHEMA)
        assert len(sent) == provider.call_count == 3
        assert all("response_format" in body for body in sent)
        assert provider._structured_output is True
        assert [call.args for call in provider_retry_sleep.await_args_list] == [(1.5,), (3.0,)]
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "status,message,param,code",
    [
        (400, "This model does not support response_format json_schema", None, None),
        (400, "Unsupported parameter", "response_format", "unsupported_parameter"),
        (
            400,
            "Invalid value: json_schema. Supported values: json_object, text",
            "response_format.type",
            "unsupported_value",
        ),
        (404, "response_format is not supported", None, None),
        (422, "Unknown parameter: response_format", None, None),
    ],
)
async def test_only_explicit_schema_rejection_switches_to_json_prompting(
    status, message, param, code, provider_retry_sleep
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(
                status, json={"error": {"message": message, "param": param, "code": code}}
            )
        return completion('{"accepted":true}')

    provider = await llm_with_transport(handle)
    try:
        assert await provider._call("報告を確認する", SCHEMA) == {"accepted": True}
        assert len(sent) == provider.call_count == 2
        assert "response_format" in sent[0] and "response_format" not in sent[1]
        assert "report_status" in sent[1]["messages"][1]["content"]
        assert provider._structured_output is False
        provider_retry_sleep.assert_not_awaited()
    finally:
        await provider.aclose()


async def test_schema_rejection_on_the_last_attempt_cannot_fabricate_an_empty_success(
    provider_retry_sleep,
):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        if len(sent) <= 2:
            return httpx.Response(503, json={"error": {"message": "temporarily busy"}})
        if len(sent) == 3:
            return httpx.Response(
                400, json={"error": {"message": "response_format json_schema is not supported"}}
            )
        return completion('{"accepted":true}')

    provider = await llm_with_transport(handle)
    try:
        with pytest.raises(APIStatusError) as error:
            await provider._call("報告を確認する", SCHEMA)
        assert error.value.status_code == 400 and len(sent) == 3
        assert provider._structured_output is False
        assert await provider._call("明示的な再試行", SCHEMA) == {"accepted": True}
        assert "response_format" not in sent[-1]
        assert provider.call_count == 4
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": -5, "completion_tokens": 1},
        {"prompt_tokens": 7, "completion_tokens": "invalid"},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 7, "completion_tokens": False},
    ],
)
async def test_bad_usage_cannot_partially_or_negatively_change_telemetry(usage):
    sent = []

    def handle(request):
        sent.append(request)
        if len(sent) == 1:
            return completion()
        body = completion().json()
        body["usage"] = usage
        return httpx.Response(200, json=body)

    provider = await llm_with_transport(handle)
    budget = TaskBudget(ExecutionLimits())
    try:
        await provider._call("事前の正常な応答")
        with pytest.raises(TaskLimitError) as error:
            async with budget.activate(lambda: None):
                await provider._call("使用量を確認する")
        assert error.value.reason == "usage_unavailable"
        assert (provider.total_prompt_tokens, provider.total_completion_tokens) == (4, 1)
        assert provider.call_count == 2
        assert budget.snapshot()["usage_complete"] is False
    finally:
        await provider.aclose()


async def test_invalid_usage_keeps_failed_history_readable_and_counts_the_actual_attempt(tmp_path):
    def handle(request):
        body = completion().json()
        body["usage"] = {"prompt_tokens": -5, "completion_tokens": 1}
        return httpx.Response(200, json=body)

    provider = await llm_with_transport(handle)
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=provider, display=FakeDisplayBackend(32, 32), store=store, decision=None
    )
    assert not await session.run("報告を保存する")
    record = TaskStore(tmp_path).load(session.id)
    assert record.state == "failed" and record.stop_reason == "usage_unavailable"
    assert record.prompt_tokens == record.completion_tokens == 0
    assert record.llm_calls == record.execution_usage["requests"] == 1
    assert record.execution_usage["usage_complete"] is False
    assert record.actions == []
