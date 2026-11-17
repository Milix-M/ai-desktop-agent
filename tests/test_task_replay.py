import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.submissions import SubmissionPendingError, fingerprint, key_digest
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient

KEY = {"Idempotency-Key": "task-retry-0000000000000001"}
BODY = {"instruction": "報告書を保存する", "vm_id": "desktop"}


@pytest.fixture
def replay_server(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    made, writes = [], []
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: FakeDisplayBackend())

    def create(display):
        original = display.type_text

        def write(text):
            writes.append(text)
            original(text)

        display.type_text = write
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)

        async def finish():
            await session._execute_recorded_action(
                Action(ActionType.TYPE, {"text": "report"}), "入力する"
            )
            session.loop.context.state = AgentState.COMPLETED

        session._execute_phase = finish
        made.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    return store, made, writes


class LostAcknowledgement(httpx.ASGITransport):
    lost = False

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        if not self.lost and request.method == "POST" and request.url.path == "/tasks":
            self.lost = True
            await response.aread()
            raise httpx.ReadError("response lost after acceptance")
        return response


async def finish_all(sessions):
    await asyncio.gather(*(s._task for s in sessions if s._task), return_exceptions=True)


async def test_lost_acknowledgement_and_retry_perform_the_action_only_once(replay_server):
    store, made, writes = replay_server
    async with AuthenticatedClient(
        transport=LostAcknowledgement(app=server.app), base_url="http://test"
    ) as api:
        with pytest.raises(httpx.ReadError):
            await api.post("/tasks", json=BODY, headers=KEY)
        await finish_all(made)
        first_id = made[0].id
        replay = await api.post("/tasks", json=BODY, headers=KEY)
        await finish_all(made)
    assert replay.status_code == 200
    assert replay.json()["session_id"] == first_id
    assert len(made) == 1
    assert writes == ["report"]
    assert len(store.list()) == 1


@pytest.mark.parametrize(
    "change", [{"instruction": "別の指示"}, {"vm_id": "other"}, {"allow_vm_restart": True}]
)
async def test_same_key_with_different_content_cannot_start_or_replace_a_task(
    replay_server, change
):
    _, made, writes = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        assert (await api.post("/tasks", json=BODY, headers=KEY)).status_code == 200
        await finish_all(made)
        response = await api.post("/tasks", json={**BODY, **change}, headers=KEY)
        await finish_all(made)
    assert response.status_code == 409
    assert len(made) == 1 and writes == ["report"]


async def test_receipt_survives_history_deletion_and_prevents_reexecution(replay_server):
    _, made, writes = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.post("/tasks", json=BODY, headers=KEY)).json()
        await finish_all(made)
        assert (await api.delete("/tasks/" + first["session_id"])).status_code == 200
        response = await api.post("/tasks", json=BODY, headers=KEY)
        await finish_all(made)
    assert response.status_code == 410
    assert len(made) == 1 and writes == ["report"]


async def test_replay_after_restart_does_not_need_a_running_vm(replay_server, monkeypatch):
    store, made, writes = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.post("/tasks", json=BODY, headers=KEY)).json()
        await finish_all(made)
        monkeypatch.setattr(server, "_store", TaskStore(store._dir.parent))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        resolve = Mock(side_effect=AssertionError("must not resolve the environment again"))
        monkeypatch.setattr(server, "_resolve_vm", resolve)
        response = await api.post("/tasks", json=BODY, headers=KEY)
    assert response.status_code == 200
    assert response.json()["session_id"] == first["session_id"]
    assert response.json()["state"] == "completed"
    assert writes == ["report"]
    resolve.assert_not_called()


async def test_concurrent_retries_share_one_session(replay_server):
    _, made, _ = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        responses = await asyncio.gather(
            *(api.post("/tasks", json=BODY, headers=KEY) for _ in range(3))
        )
        await finish_all(made)
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["session_id"] for r in responses}) == 1
    assert len(made) == 1


@pytest.mark.parametrize("key", ["short", "contains a space-000000000", "x" * 129])
async def test_invalid_retry_keys_are_rejected_before_connecting(replay_server, monkeypatch, key):
    connect = Mock()
    monkeypatch.setattr(server, "_connect_vm_display", connect)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post("/tasks", json=BODY, headers={"Idempotency-Key": key})
    assert response.status_code == 422
    connect.assert_not_called()


async def test_missing_receipt_is_repaired_from_the_accepted_task(replay_server):
    store, made, writes = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.post("/tasks", json=BODY, headers=KEY)).json()
        await finish_all(made)
        receipt = store._receipt_path(key_digest(KEY["Idempotency-Key"]))
        receipt.unlink()
        replay = await api.post("/tasks", json=BODY, headers=KEY)
        assert replay.json()["session_id"] == first["session_id"]
        assert replay.json()["replayed"] is True and receipt.is_file()
    assert len(made) == 1 and writes == ["report"]


@pytest.mark.parametrize("damaged", ["receipt", "task"])
async def test_corrupt_acceptance_data_is_never_overwritten_or_reexecuted(replay_server, damaged):
    store, made, writes = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.post("/tasks", json=BODY, headers=KEY)).json()
        await finish_all(made)
        path = (
            store._receipt_path(key_digest(KEY["Idempotency-Key"]))
            if damaged == "receipt"
            else store._dir / (first["session_id"] + ".json")
        )
        path.write_text("{broken")
        response = await api.post("/tasks", json=BODY, headers=KEY)
    assert response.status_code == 503
    assert path.read_text() == "{broken"
    assert len(made) == 1 and writes == ["report"]


async def test_receipt_failure_stops_before_model_or_display_actions(replay_server, monkeypatch):
    store, made, writes = replay_server

    def fail(record):
        raise StorageError("receipt disk full")

    monkeypatch.setattr(store, "_write_receipt", fail)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post("/tasks", json=BODY, headers=KEY)
        await finish_all(made)
    assert response.status_code == 503
    assert writes == []
    assert made[0].llm.decide_call_count == 0
    assert not made[0].display.is_connected


def test_submission_claims_are_exclusive_across_store_instances(tmp_path):
    first, second = TaskStore(tmp_path), TaskStore(tmp_path)
    digest = key_digest(KEY["Idempotency-Key"])
    request_fp = fingerprint(BODY)
    with_claim = first.claim_submission(digest, request_fp)
    try:
        with pytest.raises(SubmissionPendingError):
            second.claim_submission(digest, request_fp)
    finally:
        with_claim.close()
    second.claim_submission(digest, request_fp).close()


async def test_cancelling_a_post_keeps_its_claim_until_the_initial_write_finishes(
    replay_server, monkeypatch
):
    store, made, writes = replay_server
    entered, release = threading.Event(), threading.Event()
    original = store.save

    def save(record):
        entered.set()
        assert release.wait(3)
        original(record)

    monkeypatch.setattr(store, "save", save)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        request = asyncio.create_task(api.post("/tasks", json=BODY, headers=KEY))
        retry = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            request.cancel()
            retry = asyncio.create_task(api.post("/tasks", json=BODY, headers=KEY))
            await asyncio.sleep(0.02)
            assert not request.done() and not retry.done()
            assert len(made) == 1
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            if retry:
                response = await retry
            await finish_all(made)
    assert response.status_code == 200 and response.json()["replayed"] is True
    assert len(made) == 1 and writes == ["report"]


async def test_receipts_have_private_permissions_and_no_raw_input_or_key(replay_server):
    store, made, _ = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        await api.post("/tasks", json=BODY, headers=KEY)
        await finish_all(made)
    receipt = store._receipt_path(key_digest(KEY["Idempotency-Key"]))
    value = json.loads(receipt.read_text())
    assert value["task_id"] == made[0].id
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert KEY["Idempotency-Key"] not in receipt.read_text()
    assert BODY["instruction"] not in receipt.read_text()


async def test_key_rotation_does_not_change_the_request_fingerprint(replay_server, monkeypatch):
    _, made, writes = replay_server
    private_body = {**BODY, "instruction": "report-private-key を参照せず報告書を保存する"}
    monkeypatch.setenv("OPENAI_API_KEY", "report-private-key")
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = await api.post("/tasks", json=private_body, headers=KEY)
        await finish_all(made)
        monkeypatch.setenv("OPENAI_API_KEY", "rotated-private-key")
        replay = await api.post("/tasks", json=private_body, headers=KEY)
    assert replay.status_code == 200
    assert replay.json()["session_id"] == first.json()["session_id"]
    assert len(made) == 1 and writes == ["report"]


async def test_manual_handoff_replay_keeps_the_same_consent_request(replay_server, monkeypatch):
    _, made, _ = replay_server
    original = server._create_session

    def create(display):
        session = original(display)

        async def manual():
            await session._execute_recorded_action(
                Action(ActionType.REQUEST_USER_INPUT, {"reason": "ログインしてください"}),
                "手動入力を待つ",
            )
            session.loop.context.state = AgentState.COMPLETED

        session._execute_phase = manual
        return session

    monkeypatch.setattr(server, "_create_session", create)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.post("/tasks", json=BODY, headers=KEY)).json()
        session = made[0]
        try:
            async with asyncio.timeout(1):
                while session.manual_input is None:
                    await asyncio.sleep(0)
            pending = session.manual_input.copy()
            replay = await api.post("/tasks", json=BODY, headers=KEY)
            assert replay.status_code == 200 and replay.json()["replayed"] is True
            assert replay.json()["session_id"] == first["session_id"]
            assert replay.json()["manual_input"] == pending
            assert session.is_running and len(made) == 1
        finally:
            session.stop()
            await finish_all(made)


async def test_a_fresh_key_starts_an_intentionally_new_task_after_history_deletion(replay_server):
    _, made, writes = replay_server
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        first = (await api.post("/tasks", json=BODY, headers=KEY)).json()
        await finish_all(made)
        await api.delete("/tasks/" + first["session_id"])
        assert (await api.post("/tasks", json=BODY, headers=KEY)).status_code == 410
        fresh = await api.post(
            "/tasks", json=BODY, headers={"Idempotency-Key": KEY["Idempotency-Key"] + "-new"}
        )
        await finish_all(made)
    assert fresh.status_code == 200 and fresh.json()["replayed"] is False
    assert fresh.json()["session_id"] != first["session_id"]
    assert len(made) == 2 and writes == ["report", "report"]
