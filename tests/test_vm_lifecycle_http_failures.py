"""HTTP lifecycle failures stop their own task and preserve unrelated work."""

import asyncio

import pytest
from httpx import ASGITransport
from requests.exceptions import ReadTimeout

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from tests.http import AuthenticatedClient
from tests.test_vm_api_concurrency import environments as environments
from tests.test_vm_api_concurrency import stop_sessions


@pytest.mark.parametrize("operation", ["start", "stop", "remove"])
async def test_lost_lifecycle_ack_returns_503_without_replay_or_stopping_another_vm(
    environments, monkeypatch, operation
):
    pool, client, made = environments
    target = client.containers.get("ai-desktop-agent-desktop-1")
    other = client.containers.get("ai-desktop-agent-vm-1")
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            accepted = await api.post(
                "/tasks", json={"instruction": "他の報告を確認する", "vm_id": "vm"}
            )
            assert accepted.status_code == 200, accepted.text
            other_session = made[-1]
            own_session = None
            if operation == "start":
                target.stop()
            else:
                accepted = await api.post(
                    "/tasks", json={"instruction": "保存した報告を確認する", "vm_id": "desktop"}
                )
                assert accepted.status_code == 200, accepted.text
                own_session = made[-1]
            original = getattr(target, operation)
            calls = []

            def accepted_without_ack(*args, **kwargs):
                calls.append(target.id)
                original(*args, **kwargs)
                raise ReadTimeout("command accepted but response lost")

            monkeypatch.setattr(target, operation, accepted_without_ack)
            response = await (
                api.delete("/vms/desktop")
                if operation == "remove"
                else api.post("/vms/desktop/" + operation)
            )
            assert response.status_code == 503, response.text
            assert response.json()["detail"]
            assert calls == [target.id]
            assert other_session.is_running and other_session.display.is_connected
            assert not any((other.started, other.stopped, other.restarted, other.removed))
            if own_session:
                assert not own_session.is_running and not own_session.display.is_connected
                saved = server.get_store().load(own_session.id)
                assert saved.state == "failed" and saved.stop_reason == "cancelled"
            assert (await api.get("/tasks/" + other_session.id)).status_code == 200
        finally:
            await stop_sessions(made)
            pool.close()


async def test_a_naturally_completed_active_session_can_be_retired_without_losing_its_result(
    environments, monkeypatch
):
    pool, _client, made = environments

    def create(display):
        session = TaskSession(
            llm=MockLLMProvider(
                decide_result=ActionDecision(
                    Action(ActionType.SUBTASK_COMPLETE), "done", 1.0, "done"
                )
            ),
            display=display,
            store=server.get_store(),
            decision=None,
        )
        made.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            response = await api.post(
                "/tasks", json={"instruction": "報告の確認を終える", "vm_id": "desktop"}
            )
            assert response.status_code == 200, response.text
            session = made[-1]
            await asyncio.wait_for(asyncio.shield(session._task), 5)
            # Completion removes the live registry entry through the real done callback.
            await asyncio.sleep(0)
            assert server._active_session is session and session.id not in server._sessions
            before = server.get_store().load(session.id).to_dict()
            assert before["state"] == "completed"
            response = await api.post("/vms/desktop/stop")
            assert response.status_code == 200, response.text
            assert server._active_session is None and not session.display.is_connected
            assert server.get_store().load(session.id).to_dict() == before
        finally:
            await stop_sessions(made)
            pool.close()
