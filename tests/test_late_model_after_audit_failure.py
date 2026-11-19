"""A late provider result cannot authorize input after a control audit write fails."""

import asyncio

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_a_provider_returning_after_cancellation_cannot_bypass_the_failed_audit(tmp_path):
    deciding, returned_late = asyncio.Event(), asyncio.Event()

    class SlowCancellationModel(MockLLMProvider):
        async def decide_next_action(self, *args, **kwargs):
            deciding.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A provider adapter may finish an already accepted request even
                # after cancellation. Its valid result still needs authorization.
                returned_late.set()
                return ActionDecision(
                    Action(ActionType.KEY_PRESS, {"key": "escape"}),
                    "close the document menu",
                    1.0,
                    "menu closes",
                )

    class FailingPauseStore(TaskStore):
        failed_writes = 0

        def save(self, record):
            if record.state == "paused":
                self.failed_writes += 1
                raise OSError("control audit write failed")
            return super().save(record)

    store = FailingPauseStore(tmp_path)
    display = FakeDisplayBackend(96, 64)
    session = TaskSession(llm=SlowCancellationModel(), display=display, store=store, decision=None)
    completed = []
    session.on_complete(completed.append)
    run = asyncio.create_task(session.run("保存済み文書を確認する"))
    try:
        await asyncio.wait_for(deciding.wait(), 2)
        paused = session.pause()
        with pytest.raises(StorageError):
            await asyncio.wait_for(paused, 2)
        await asyncio.wait_for(returned_late.wait(), 2)
        resumed = session.resume()
        with pytest.raises(StorageError):
            await asyncio.wait_for(resumed, 2)
        assert await asyncio.wait_for(asyncio.shield(run), 2) is False
        saved = TaskStore(tmp_path).load(session.id)
        assert saved.state == "failed" and saved.success is False
        assert saved.stop_reason == "storage_error" and "保存" in saved.error_message
        assert saved.actions == [] and completed == [False]
        assert store.failed_writes == 1
        assert display.key_presses == display.clicks == display.text_inputs == []
        assert not display.is_connected and not session.is_running
    finally:
        session.stop()
        await asyncio.gather(run, return_exceptions=True)
        await session.aclose()
