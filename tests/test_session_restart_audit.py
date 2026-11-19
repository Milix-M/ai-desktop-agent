"""Restart attempts retain their model evidence, duration and resource ownership."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import docker
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, RecoveryPlan, RecoveryStrategy
from ai_desktop_agent.server import session as session_module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def restarting_session(tmp_path):
    decision = ActionDecision(
        Action(ActionType.VM_RESTART, {"reason": "アプリが応答しない"}),
        "保存済みの作業から再開する",
        0.63,
        "入力と待機で復旧しなかったため再起動を選んだ",
    )
    model = MockLLMProvider(
        decide_result=decision,
        recover_result=RecoveryPlan(RecoveryStrategy.GIVE_UP, [], "再起動結果を報告する", False),
    )
    session = TaskSession(
        llm=model,
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=None,
        vm_id="desk-owned",
    )
    session.allow_vm_restart = True
    return session, decision


@pytest.mark.parametrize("outcome", ["success", "failure", "denied"])
async def test_restart_records_the_original_model_evidence_and_owns_its_client(
    tmp_path, monkeypatch, outcome
):
    session, decision = restarting_session(tmp_path)
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(docker, "from_env", factory)
    monkeypatch.setattr(session_module, "RESTART_SETTLE_SECONDS", 0)

    def restart(pool, vm_id):
        assert vm_id == session.vm_id and pool._docker() is client
        client.close.assert_not_called()
        if outcome == "failure":
            raise ConnectionError("restart response lost")
        return SimpleNamespace(id=vm_id)

    monkeypatch.setattr(VmPool, "restart_vm", restart)
    session.allow_vm_restart = outcome != "denied"
    assert await session.run("文書を保存して応答しない環境を復旧する") is False
    record = session._store.load(session.id)
    assert len(record.actions) == 1
    action = record.actions[0]
    assert action.reasoning == decision.reasoning and action.confidence == decision.confidence
    assert action.success is (outcome == "success")
    assert record.state == "failed" and not session.display.is_connected
    if outcome == "denied":
        factory.assert_not_called()
        assert "許可" in action.error_message
    else:
        client.close.assert_called_once()
        assert action.duration_ms > 0
        if outcome == "failure":
            assert "restart response lost" in action.error_message


async def test_cancelling_restart_keeps_the_partial_action_and_closes_after_the_worker(
    tmp_path, monkeypatch
):
    session, decision = restarting_session(tmp_path)
    entered, release = threading.Event(), threading.Event()
    client = Mock()
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    def restart(pool, vm_id):
        assert pool._docker() is client
        entered.set()
        assert release.wait(3)
        client.close.assert_not_called()
        return SimpleNamespace(id=vm_id)

    monkeypatch.setattr(VmPool, "restart_vm", restart)
    await session.start_async("文書を保存して応答しない環境を復旧する")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            session.stop()
            await asyncio.sleep(0)
        assert not session._task.done()
        client.close.assert_not_called()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(session._task), 2)
        client.close.assert_called_once()
        record = session._store.load(session.id)
        action = record.actions[0]
        assert action.reasoning == decision.reasoning and action.confidence == decision.confidence
        assert not action.success and "中断" in action.error_message and action.duration_ms > 0
        assert record.state == "failed" and not session.display.is_connected
    finally:
        release.set()
        await asyncio.gather(session._task, return_exceptions=True)
