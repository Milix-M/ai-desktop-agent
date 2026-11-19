import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server import app as server_app
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


def manual_session(tmp_path, **kwargs):
    model = MockLLMProvider()
    model.understand_instruction = AsyncMock(
        return_value=UnderstandingResult("login", None, [], "手動入力を待つ")
    )
    model.decompose_task = AsyncMock(
        return_value=DecompositionResult([Subtask("login", "ログインする")], "計画")
    )
    model.decide_next_action = AsyncMock(
        side_effect=[
            ActionDecision(
                Action(ActionType.REQUEST_USER_INPUT, {"reason": "パスワードを入力してください"}),
                "入力を待つ",
                1,
                "利用者が入力する",
            ),
            ActionDecision(
                Action(ActionType.SUBTASK_COMPLETE), "ログイン完了", 1, "画面を確認した"
            ),
        ]
    )
    model.verify_result = AsyncMock(
        return_value=VerificationResult(True, "ログイン完了", "確認画面")
    )
    session = TaskSession(
        llm=model, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None, **kwargs
    )
    session.vm_id = "desktop"
    return session


async def wait_for_manual(session):
    async with asyncio.timeout(3):
        while session.manual_input is None:
            if session._task is not None and session._task.done():
                pytest.fail("Task exited before requesting manual input")
            await asyncio.sleep(0.01)
    return dict(session.manual_input)


async def test_manual_input_blocks_screen_and_model_calls_until_explicit_consent(
    tmp_path, monkeypatch
):
    session = manual_session(tmp_path)
    session.display.capture_screen = MockCapture(session.display.capture_screen)
    monkeypatch.setattr(server_app, "_sessions", {session.id: session})
    monkeypatch.setattr(server_app, "_active_session", session)
    await session.start_async("ログインする。秘密の値は利用者が入力する")
    try:
        pending = await wait_for_manual(session)
        calls = session.llm.decide_next_action.call_count
        captures = session.display.capture_screen.calls
        await asyncio.sleep(0.05)
        assert session.llm.decide_next_action.call_count == calls
        assert session.display.capture_screen.calls == captures
        with pytest.raises(RuntimeError, match="手動入力中"):
            await session._capture_screenshot()
        assert session.display.capture_screen.calls == captures
        assert session.loop.state == AgentState.AWAITING_USER
        async with AuthenticatedClient(
            transport=ASGITransport(app=server_app.app), base_url="http://test"
        ) as client:
            status = (await client.get("/tasks/current?vm_id=desktop")).json()
            assert status["manual_input"] == pending and status["is_running"]
            response = await client.post(
                "/tasks/current/resume",
                params={"vm_id": "desktop", "session_id": "previous-task"},
                json={"manual_input_id": pending["id"], "allow_screen_share": True},
            )
            assert response.status_code == 409
            assert session.loop.state == AgentState.AWAITING_USER
            assert session.llm.decide_next_action.call_count == calls
            assert session.display.capture_screen.calls == captures
            for body in [
                None,
                {"manual_input_id": pending["id"]},
                {"manual_input_id": "stale", "allow_screen_share": True},
            ]:
                response = await client.post("/tasks/current/resume?vm_id=desktop", json=body)
                assert response.status_code == 409
                assert session.loop.state == AgentState.AWAITING_USER
            response = await client.post(
                "/tasks/current/resume",
                params={"vm_id": "desktop", "session_id": session.id},
                json={"manual_input_id": pending["id"], "allow_screen_share": True},
            )
            assert response.status_code == 200
        assert await asyncio.wait_for(session._task, 4)
        assert session.manual_input is None
        record = session._store.load(session.id)
        assert record.state == "completed"
        assert record.actions[0].action_type == "request_user_input" and record.actions[0].success
        assert session.llm.decide_next_action.call_count > calls
    finally:
        session.stop()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_new_task_cannot_replace_a_session_waiting_for_private_input(tmp_path, monkeypatch):
    session = manual_session(tmp_path)
    monkeypatch.setattr(server_app, "_sessions", {session.id: session})
    monkeypatch.setattr(server_app, "_active_session", session)
    monkeypatch.setattr(server_app, "_store", session._store)
    monkeypatch.setattr(server_app, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server_app, "_default_vm_id", lambda: "desktop")
    connect = Mock()
    monkeypatch.setattr(server_app, "_connect_vm_display", connect)
    await session.start_async("ログインする")
    try:
        pending = await wait_for_manual(session)
        async with AuthenticatedClient(
            transport=ASGITransport(app=server_app.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/tasks", json={"instruction": "画面を確認する", "vm_id": "desktop"}
            )
        assert response.status_code == 409
        connect.assert_not_called()
        assert session.manual_input == pending and session.is_running
    finally:
        session.stop()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_manual_handoff_during_new_display_connection_still_blocks_replacement(
    tmp_path, monkeypatch
):
    previous = SimpleNamespace(vm_id="desktop", is_running=True, manual_input=None, stop=Mock())
    display = FakeDisplayBackend()
    disconnect = Mock(wraps=display.disconnect)
    display.disconnect = disconnect
    monkeypatch.setattr(server_app, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server_app, "_sessions", {"previous": previous})
    monkeypatch.setattr(server_app, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server_app, "_default_vm_id", lambda: "desktop")

    def connect(_):
        previous.manual_input = {"id": "pending", "reason": "パスワードを入力してください"}
        return display

    monkeypatch.setattr(server_app, "_connect_vm_display", connect)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server_app.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/tasks", json={"instruction": "画面を確認する", "vm_id": "desktop"}
        )
    assert response.status_code == 409
    previous.stop.assert_not_called()
    disconnect.assert_called_once()


class MockCapture:
    def __init__(self, capture):
        self.capture = capture
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return self.capture(**kwargs)


async def test_stop_while_waiting_records_an_interruption_and_closes_connections(tmp_path):
    session = manual_session(tmp_path)
    await session.start_async("ログインする")
    await wait_for_manual(session)
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await session._task
    record = session._store.load(session.id)
    assert record.state == "failed" and not record.actions[0].success
    assert session.manual_input is None and not session.display.is_connected


@pytest.mark.parametrize("exit_mode", ["resume", "stop", "deadline"])
@pytest.mark.parametrize("release_fails", [False, True], ids=["released", "release-failed"])
async def test_input_release_finishes_before_agent_continues_or_session_exits(
    tmp_path, monkeypatch, exit_mode, release_fails
):
    from ai_desktop_agent.server.vnc_input import VncInput

    session = manual_session(tmp_path, limits=ExecutionLimits(max_seconds=600))
    session.display.capture_screen = MockCapture(session.display.capture_screen)
    event_loop = asyncio.get_running_loop()
    schedule = event_loop.call_later
    deadlines = []

    def record_deadline(delay, callback, *args, **kwargs):
        timer = schedule(delay, callback, *args, **kwargs)
        if delay == 600:
            deadlines.append((timer, callback, args))
        return timer

    monkeypatch.setattr(event_loop, "call_later", record_deadline)
    await session.start_async("ログインする")
    pending = await wait_for_manual(session)
    release_started, unblock = asyncio.Event(), asyncio.Event()

    async def release():
        release_started.set()
        await unblock.wait()
        if release_fails:
            raise ConnectionError("desktop connection lost")

    writer = Mock(drain=AsyncMock(side_effect=release))
    viewer = VncInput(writer, lambda: session._input_authority)
    viewer.forward(b"RFB 003.008\n\x01\x01\x04\x01\0\0\0\0\xff\xe3")
    calls = session.llm.decide_next_action.call_count
    captures = session.display.capture_screen.calls
    try:
        if exit_mode == "resume":
            session.resume(manual_input_id=pending["id"], allow_screen_share=True)
        elif exit_mode == "stop":
            session.stop()
        else:
            assert len(deadlines) == 1
            timer, expire, args = deadlines[0]
            timer.cancel()
            expire(*args)
        await asyncio.wait_for(release_started.wait(), 2)
        if exit_mode == "stop":
            session.stop()  # Repeated cancellation must not abandon input cleanup.
        assert not session._task.done()
        assert session.llm.decide_next_action.call_count == calls
        assert session.display.capture_screen.calls == captures
        assert session.manual_input is None and session._input_authority is None
        assert writer.write.call_args.args == (b"\x04\0\0\0\0\0\xff\xe3",)
        unblock.set()
        result = (await asyncio.wait_for(asyncio.gather(session._task, return_exceptions=True), 3))[
            0
        ]
        if exit_mode == "resume" and not release_fails:
            assert result is True
            assert session.llm.decide_next_action.call_count > calls
        else:
            assert result is False or isinstance(result, asyncio.CancelledError)
            assert session.llm.decide_next_action.call_count == calls
            assert session.display.capture_screen.calls == captures
            assert session._store.load(session.id).state == "failed"
    finally:
        unblock.set()
        session.stop()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_time_budget_also_expires_while_waiting_for_manual_input(tmp_path):
    session = manual_session(tmp_path, limits=ExecutionLimits(max_seconds=0.2))
    session._capture_screenshot = AsyncMock(return_value=session.display.capture_screen())
    await session.start_async("ログインする")
    await wait_for_manual(session)
    assert not await asyncio.wait_for(session._task, 2)
    record = session._store.load(session.id)
    assert record.stop_reason == "duration_limit" and record.state == "failed"
    assert session.manual_input is None


async def test_recovery_can_request_input_and_stale_approval_cannot_release_next_request(tmp_path):
    session = manual_session(tmp_path)
    session.loop.start(Goal("ログインする"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("login", "ログインする")])
    session.loop.action_executed()
    session.loop.wait_complete()
    session.loop.verify_failed()
    plan = RecoveryPlan(
        RecoveryStrategy.ALTERNATIVE_APPROACH,
        [Action(ActionType.REQUEST_USER_INPUT, {"reason": "認証コードを入力してください"})],
        "認証待ち",
        True,
    )
    previous_id = None
    for _ in range(2):
        task = asyncio.create_task(session._execute_recovery_actions(plan, 0))
        pending = await wait_for_manual(session)
        if previous_id is not None:
            with pytest.raises(ValueError):
                session.resume(manual_input_id=previous_id, allow_screen_share=True)
        session.resume(manual_input_id=pending["id"], allow_screen_share=True)
        assert await asyncio.wait_for(task, 1)
        assert session.loop.state == AgentState.RECOVERING
        previous_id = pending["id"]


async def test_direct_executor_never_treats_manual_input_as_success():
    executor = ActionExecutor(FakeDisplayBackend())
    assert not await executor.execute(
        Action(ActionType.REQUEST_USER_INPUT, {"reason": "入力してください"})
    )
    assert "タスクセッション" in executor.last_error


@pytest.mark.parametrize("reason", ["", "   ", 1, "a" * 1001])
def test_invalid_manual_requests_fail_before_waiting(reason):
    with pytest.raises(ValueError):
        Action(ActionType.REQUEST_USER_INPUT, {"reason": reason})


def test_restart_does_not_resume_a_task_waiting_for_private_input(tmp_path):
    store = TaskStore(tmp_path)
    session = manual_session(tmp_path)
    session.loop.start(Goal("ログインする"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("login", "ログインする")])
    session.loop.wait_for_user()
    store.save(session.snapshot())
    assert store.mark_interrupted() == 1
    assert store.load(session.id).state == "interrupted"


async def test_approval_for_another_vm_cannot_release_private_input(tmp_path, monkeypatch):
    first = manual_session(tmp_path)
    second = manual_session(tmp_path)
    second.vm_id = "other-desktop"
    monkeypatch.setattr(server_app, "_sessions", {first.id: first, second.id: second})
    monkeypatch.setattr(server_app, "_active_session", second)
    await first.start_async("ログインする")
    await second.start_async("ログインする")
    try:
        await wait_for_manual(first)
        pending = await wait_for_manual(second)
        async with AuthenticatedClient(
            transport=ASGITransport(app=server_app.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/tasks/current/resume?vm_id=desktop",
                json={"manual_input_id": pending["id"], "allow_screen_share": True},
            )
        assert response.status_code == 409
        assert first.manual_input is not None and second.manual_input is not None
    finally:
        first.stop()
        second.stop()
        await asyncio.gather(first._task, second._task, return_exceptions=True)


async def test_model_schema_and_parser_can_request_manual_input():
    from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider

    provider = OpenAICompatProvider(api_key="test-key")
    provider._call = AsyncMock(
        return_value={
            "action_type": "request_user_input",
            "params": {"reason": "認証コードを入力してください"},
            "expected_effect": "認証",
            "confidence": 1.0,
            "reasoning": "利用者が入力する",
        }
    )
    try:
        decision = await provider.decide_next_action(
            Goal("ログインする"),
            Subtask("login", "ログインする"),
            [],
            FakeDisplayBackend().capture_screen(),
        )
        assert decision.action.action_type == ActionType.REQUEST_USER_INPUT
        schema = provider._call.call_args.args[1]["schema"]["properties"]["action"]["anyOf"]
        assert any(
            item["properties"]["action_type"]["const"] == "request_user_input" for item in schema
        )
    finally:
        await provider.aclose()
