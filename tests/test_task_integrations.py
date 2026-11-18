import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.webhook import TaskWebhook
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient

SECRET = "webhook-test-secret-0123456789abcdef"


async def test_task_result_survives_reload_and_is_signed_for_webhook(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=MockLLMProvider(
            decide_result=ActionDecision(
                Action(
                    ActionType.SUBTASK_COMPLETE,
                    {"summary": "発送待ち0件", "data_json": '{"count":0}'},
                ),
                "画面確認",
                1.0,
                "発送待ち一覧が空",
            )
        ),
        display=FakeDisplayBackend(),
        store=store,
        decision=None,
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm_id: SimpleNamespace(id=vm_id))
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: session.display)
    monkeypatch.setattr(server, "_create_session", lambda _: session)

    deliveries = []
    delivered = asyncio.Event()

    def receive(request):
        deliveries.append(request)
        if len(deliveries) == 1:
            return httpx.Response(503)
        delivered.set()
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(receive)) as outgoing,
        AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as api,
    ):
        webhook = TaskWebhook("https://receiver.example.com/desktop", SECRET, outgoing)
        monkeypatch.setattr(server, "_task_webhook", webhook)
        delivery = asyncio.create_task(webhook.run())
        try:
            response = await api.post(
                "/tasks", json={"instruction": "発送待ちの件数を返す", "vm_id": "desktop"}
            )
            assert response.status_code == 200
            assert await asyncio.wait_for(session._task, 5)
            await asyncio.wait_for(delivered.wait(), 5)
            detail = (await api.get(f"/tasks/{response.json()['session_id']}")).json()
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

    first, request = deliveries
    assert first.content == request.content and first.headers == request.headers
    payload = json.loads(request.content)
    assert payload["event_id"] == request.headers["X-Desktop-Agent-Event-Id"]
    assert (
        payload["result"] == detail["result"] == [{"summary": "発送待ち0件", "data": {"count": 0}}]
    )
    assert payload["state"] == detail["state"] == "completed"
    assert payload["task_id"] == detail["id"]
    assert request.headers["X-Desktop-Agent-Signature"] == (
        "sha256=" + hmac.new(SECRET.encode(), request.content, hashlib.sha256).hexdigest()
    )
    assert "instruction" not in payload and "actions" not in payload


async def test_mcp_submission_cannot_stop_an_existing_task(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    active = SimpleNamespace(vm_id="desktop", is_running=True, stop=Mock())
    monkeypatch.setattr(server, "_sessions", {"active": active})
    monkeypatch.setattr(server, "_resolve_vm", lambda vm_id: SimpleNamespace(id=vm_id))
    connect = Mock()
    monkeypatch.setattr(server, "_connect_vm_display", connect)

    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post(
            "/tasks",
            headers={"Idempotency-Key": "replay-000000000001"},
            json={"instruction": "件数を返す", "vm_id": "desktop", "replace_running": False},
        )
    assert response.status_code == 409
    active.stop.assert_not_called()
    connect.assert_not_called()
