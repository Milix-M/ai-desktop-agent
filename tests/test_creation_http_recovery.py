"""Keep legacy creation retries and recovery failures safe at the HTTP boundary."""

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.kvm import KvmUnavailableError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.vm.async_io import reserve_vm_creation_slot
from tests.http import AuthenticatedClient
from tests.test_vm_creations import KEY, receipt
from tests.test_vm_creations import managed as managed


@pytest.mark.parametrize(
    "error,status",
    [
        (KvmUnavailableError(), 409),
        (DockerUnavailableError("daemon unavailable"), 503),
        (ValueError("invalid creation settings"), 400),
        (RuntimeError("internal implementation detail"), 503),
    ],
)
async def test_legacy_keyed_failure_is_replayed_without_allocating_and_next_key_can_succeed(
    managed, monkeypatch, error, status
):
    service, pool, client = managed
    monkeypatch.setattr(server, "_vm_creations", service)
    create, attempts = pool.create_vm, []

    def fail_once(*args, **kwargs):
        attempts.append(kwargs["vm_id"])
        if len(attempts) == 1:
            raise error
        return create(*args, **kwargs)

    monkeypatch.setattr(pool, "create_vm", fail_once)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        for _ in range(2):
            failed = await api.post(
                "/vms", json={"kind": "container"}, headers={"Idempotency-Key": KEY}
            )
            assert failed.status_code == status, failed.text
            assert failed.json()["detail"]
            assert "internal implementation detail" not in failed.text
        assert len(attempts) == 1 and client.containers.runs == []
        completed = await api.post(
            "/vms", json={"kind": "container"}, headers={"Idempotency-Key": KEY + "-next"}
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["id"] != attempts[0]
        assert len(attempts) == 2 and len(client.containers.runs) == 1


async def test_legacy_key_cannot_change_an_already_created_environment(managed, monkeypatch):
    service, _, client = managed
    monkeypatch.setattr(server, "_vm_creations", service)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        headers = {"Idempotency-Key": KEY}
        first = await api.post(
            "/vms", json={"kind": "container", "name": "作業環境"}, headers=headers
        )
        assert first.status_code == 200, first.text
        conflict = await api.post(
            "/vms", json={"kind": "container", "name": "変更した環境"}, headers=headers
        )
        assert conflict.status_code == 409, conflict.text
        retry = await api.post(
            "/vms", json={"kind": "container", "name": "作業環境"}, headers=headers
        )
        assert retry.status_code == 200 and retry.json() == first.json()
        assert len(client.containers.runs) == 1


@pytest.mark.parametrize("route", ["legacy", "submit", "get"])
async def test_shutdown_refuses_creation_requests_without_publishing_or_allocating(
    managed, monkeypatch, route
):
    service, _, client = managed
    monkeypatch.setattr(server, "_vm_creations", service)
    await service.close()
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        if route == "get":
            response = await api.get("/vm-creations/" + receipt().id)
        else:
            response = await api.post(
                "/vms" if route == "legacy" else "/vm-creations",
                json={"kind": "container"},
                headers={"Idempotency-Key": KEY},
            )
        assert response.status_code == 503 and "終了" in response.json()["detail"]
        assert client.containers.runs == []
        assert not list(service.store.directory.glob("creation-*.json"))


async def test_receipt_query_reports_recovery_backlog_and_retries_after_capacity_is_released(
    managed, monkeypatch
):
    service, _, client = managed
    monkeypatch.setattr(server, "_vm_creations", service)
    saved = receipt()
    service.store.write(saved)
    path = service.store.directory / (saved.id + ".json")
    original = path.read_bytes()
    # These are real shared backlog reservations, also used by legacy creation calls.
    slots = [reserve_vm_creation_slot() for _ in range(8)]
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            busy = await api.get("/vm-creations/" + saved.id)
            assert busy.status_code == 429 and busy.headers["Retry-After"] == "1"
            assert path.read_bytes() == original and client.containers.runs == []
            for slot in slots:
                slot.release()
            retry = await api.get("/vm-creations/" + saved.id)
            assert retry.status_code == 200, retry.text
            completed = await service.wait(saved.id)
            assert completed.state == "succeeded" and completed.vm_id == saved.vm_id
            assert len(client.containers.runs) == 1
            again = await api.get("/vm-creations/" + saved.id)
            assert again.json()["state"] == "succeeded"
            assert len(client.containers.runs) == 1
    finally:
        for slot in slots:
            slot.release()


@pytest.mark.parametrize("damage", ["removed", "restored-queued"])
async def test_a_disrupted_creation_receipt_cannot_report_success_or_duplicate_the_environment(
    managed, monkeypatch, damage
):
    import asyncio
    import threading

    service, pool, client = managed
    monkeypatch.setattr(server, "_vm_creations", service)
    completed, release = threading.Event(), threading.Event()
    write = service.store.write

    def delayed_publication(record):
        write(record)
        if record.state == "succeeded":
            completed.set()
            assert release.wait(5)

    monkeypatch.setattr(service.store, "write", delayed_publication)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        request = asyncio.create_task(
            api.post(
                "/vms",
                json={"kind": "container", "name": "作業環境"},
                headers={"Idempotency-Key": KEY},
            )
        )
        try:
            assert await asyncio.to_thread(completed.wait, 2)
            saved = receipt()
            path = service.store.directory / (saved.id + ".json")
            if damage == "removed":
                path.unlink()
            else:
                # Restore a valid older receipt, rather than fabricating an invalid success DTO.
                service.store.write(saved)
            release.set()
            missing = await asyncio.wait_for(asyncio.shield(request), 3)
            assert missing.status_code == 503
            assert ("記録" if damage == "removed" else "結果") in missing.json()["detail"]
            assert pool.get_vm(saved.vm_id) is not None and len(client.containers.runs) == 1
            receipt_response = await api.get("/vm-creations/" + saved.id)
            if damage == "removed":
                assert receipt_response.status_code == 404
            else:
                assert receipt_response.status_code == 200
                assert receipt_response.json()["state"] == "queued"
                assert receipt_response.json()["result"] is None
            retry = await api.post(
                "/vms",
                json={"kind": "container", "name": "作業環境"},
                headers={"Idempotency-Key": KEY},
            )
            assert retry.status_code == 400 and "同じID" in retry.json()["detail"]
            assert len(client.containers.runs) == 1
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
