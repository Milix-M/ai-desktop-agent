"""Ambiguous JSON must not be rescued into an action or a completion decision."""

import json

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_task_budget import completion, llm_with_transport

WRAPPERS = ["{}", "```json\n{}\n```", "```\n{}", "結果: {} です"]


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize(
    "content",
    [
        '{"success":false,"success":true}',
        '{"success":true,"success":true}',
        '{"action_type":"wait","action_type":"left_click"}',
        '{"params":{"x":1,"x":200,"y":10}}',
        r'{"params":{"x":1,"\u0078":200,"y":10}}',
        '{"actions":[{"params":{"text":"first","text":"second"}}]}',
    ],
)
def test_repeated_keys_are_rejected_at_every_depth_and_through_text_fallback(content, wrapper):
    with pytest.raises(ValueError):
        OpenAICompatProvider._parse_json(wrapper.format(content))


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize(
    "content",
    [
        '[{"success":true}]',
        '[false,{"success":true}]',
        '[{"success":true},false]',
        '[[{"success":true}]]',
        '[{"success":true}',
        '"{}"',
        '"{\\"success\\":true}"',
    ],
)
def test_nested_objects_cannot_be_extracted_from_nonobject_or_broken_json_roots(content, wrapper):
    with pytest.raises(ValueError):
        OpenAICompatProvider._parse_json(wrapper.format(content))


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_unique_objects_retain_nested_values_and_identical_keys_in_separate_objects(wrapper):
    value = {"success": False, "first": {"id": 1}, "second": {"id": 2}, "items": [{}]}
    assert OpenAICompatProvider._parse_json(wrapper.format(json.dumps(value))) == value


@pytest.fixture
async def wire():
    state = {"content": "{}", "requests": []}

    async def handle(request):
        state["requests"].append(json.loads(request.content))
        responses = state.get("responses", [])
        return completion(responses.pop(0) if responses else state["content"])

    provider = await llm_with_transport(handle)
    try:
        yield provider, state
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "content",
    [
        '{"action_type":"left_click","params":{"x":1,"x":10,"y":20},"confidence":1}',
        '[false,{"action_type":"left_click","params":{"x":10,"y":20},"confidence":1}]',
        "",
        '{"action_type":',
    ],
)
async def test_unusable_sdk_responses_never_reach_the_desktop_or_multiply_provider_retries(
    wire, content, provider_retry_sleep, session_retry_sleep
):
    provider, state = wire
    state["content"] = content
    llm = MockLLMProvider()
    llm.decide_next_action = provider.decide_next_action
    display = FakeDisplayBackend(32, 32)
    session = TaskSession(llm=llm, display=display, decision=None)
    session.loop.start(Goal("保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("s1", "保存する")])
    try:
        with pytest.raises(ValueError):
            await session._execute_phase()
        assert display.clicks == []
        assert display.key_presses == []
        assert display.text_inputs == []
        assert session.loop.context.action_history == []
        assert len(state["requests"]) == 3
        assert [call.args for call in provider_retry_sleep.await_args_list] == [(1.5,), (3.0,)]
        session_retry_sleep.assert_not_awaited()
    finally:
        await session.aclose()


@pytest.mark.parametrize(
    "invalid",
    [
        '{"action_type":"left_click","params":{"x":1,"x":10,"y":20},"confidence":1}',
        '[false,{"action_type":"left_click","params":{"x":10,"y":20},"confidence":1}]',
    ],
)
async def test_a_valid_provider_retry_executes_only_the_new_unambiguous_action(
    wire, invalid, provider_retry_sleep, session_retry_sleep
):
    provider, state = wire
    state["responses"] = [
        invalid,
        '{"action_type":"type","params":{"text":"report"},"confidence":1}',
    ]
    llm = MockLLMProvider()
    llm.decide_next_action = provider.decide_next_action
    display = FakeDisplayBackend(32, 32)
    session = TaskSession(llm=llm, display=display, decision=None)
    session.loop.start(Goal("保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("s1", "保存する")])
    try:
        await session._execute_phase()
        assert display.clicks == []
        assert display.text_inputs == ["report"]
        assert len(session.loop.context.action_history) == 1
        assert len(state["requests"]) == 2
        assert [call.args for call in provider_retry_sleep.await_args_list] == [(1.5,)]
        session_retry_sleep.assert_not_awaited()
    finally:
        await session.aclose()


@pytest.mark.parametrize(
    "content", ['{"success":false,"success":true}', '[false,{"success":true}]']
)
async def test_ambiguous_sdk_verification_cannot_advance_a_subtask(
    wire, content, provider_retry_sleep, session_retry_sleep, tmp_path
):
    provider, state = wire
    state["content"] = content
    llm = MockLLMProvider()
    llm.verify_result = provider.verify_result
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=FakeDisplayBackend(32, 32), store=store, decision=None)
    try:
        assert await session.run("保存する") is False
        assert session.loop.state == AgentState.FAILED
        assert session.loop.context.current_subtask_index == 0
        record = store.load(session.id)
        assert record.stop_reason == "execution_error" and record.error_message
        assert not session.display.is_connected
        assert len(state["requests"]) == 3
        assert [call.args for call in provider_retry_sleep.await_args_list] == [(1.5,), (3.0,)]
    finally:
        await session.aclose()
