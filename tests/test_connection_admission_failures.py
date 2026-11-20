"""A VNC handshake failure cannot consume a task retry key or leave live ownership."""

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport
from twisted.internet.error import ConnectionRefusedError
from twisted.python.failure import Failure
from vncdotool import api as vnc_api

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_transport import ManagedVNCFactory, ManagedVNCProxy
from tests.http import AuthenticatedClient
from tests.test_vnc_transport import connected as connected


@pytest.mark.parametrize("keyed", [False, True], ids=["legacy", "retry-key"])
async def test_failed_vnc_admission_returns_502_and_the_same_request_can_recover(
    tmp_path, monkeypatch, connected, keyed
):
    successful_proxy, protocol = connected
    store = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(
        server, "_resolve_vm", lambda vm: SimpleNamespace(id=vm, vnc_host="desktop")
    )
    attempts, failed_proxies, made = [], [], []
    completed = asyncio.Event()

    def connect(*args, **kwargs):
        attempts.append(args[0])
        if len(attempts) == 1:
            factory = ManagedVNCFactory()
            proxy = ManagedVNCProxy(factory, timeout=0.1)
            failed_proxies.append(proxy)
            factory.clientConnectionFailed(None, Failure(ConnectionRefusedError("refused")))
            return proxy
        return successful_proxy

    def create(display):
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)
        session.on_complete(lambda _: completed.set())
        made.append(session)
        return session

    monkeypatch.setattr(vnc_api, "connect", connect)
    monkeypatch.setattr(server, "_create_session", create)
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            payload = {"instruction": "文書を確認する", "vm_id": "desktop"}
            headers = {"Idempotency-Key": "vnc-recovery-request-0123456789"} if keyed else {}
            failed = await client.post("/tasks", json=payload, headers=headers)
            assert failed.status_code == 502 and "VNC接続に失敗" in failed.json()["detail"]
            assert len(attempts) == 1 and failed_proxies[0].closed
            assert not made and store.list(limit=None) == []
            assert server._sessions == {} and server._active_session is None

            accepted = await client.post("/tasks", json=payload, headers=headers)
            assert accepted.status_code == 200, accepted.text
            await asyncio.wait_for(completed.wait(), 3)
            await made[0].aclose()
            record = store.load(accepted.json()["session_id"])
            assert record.state == "completed" and record.success is True
            assert not made[0].is_running and not made[0].display.is_connected
            assert len(store.list(limit=None)) == 1 and len(made) == 1
            assert attempts == ["desktop::5900", "desktop::5900"]
            assert protocol.values == []
            if keyed:
                replay = await client.post("/tasks", json=payload, headers=headers)
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                assert replay.json()["session_id"] == record.id and len(attempts) == 2
    finally:
        for session in made:
            await session.aclose()
