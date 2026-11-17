import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.server import app as server
from tests.http import AuthenticatedClient
from tests.test_vm_creations import KEY, receipt
from tests.test_vm_creations import managed as managed


@pytest.fixture
async def api(managed, monkeypatch):
    service, pool, _ = managed
    monkeypatch.setattr(server, "_vm_creations", service)
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        yield client


async def test_async_creation_returns_a_durable_pollable_receipt(api, managed):
    service, _, docker = managed
    accepted = await api.post(
        "/vm-creations",
        json={"kind": "container", "name": "作業環境"},
        headers={"Idempotency-Key": KEY},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["id"]
    assert accepted.headers["Location"] == "/vm-creations/" + job_id
    assert accepted.headers["Cache-Control"] == "no-store"
    assert "fingerprint" not in accepted.json() and KEY not in accepted.text
    await service.wait(job_id)
    result = await api.get("/vm-creations/" + job_id)
    assert result.status_code == 200 and result.json()["state"] == "succeeded"
    assert result.headers["Cache-Control"] == "no-store"
    replay = await api.post(
        "/vm-creations",
        json={"kind": "container", "name": "作業環境"},
        headers={"Idempotency-Key": KEY},
    )
    assert replay.status_code == 200 and replay.json()["id"] == job_id
    assert len(docker.containers.runs) == 1


async def test_concurrent_http_retries_and_changed_payload(api, managed):
    service, _, docker = managed
    responses = await asyncio.gather(
        *[
            api.post("/vm-creations", json={"kind": "container"}, headers={"Idempotency-Key": KEY})
            for _ in range(5)
        ]
    )
    assert all(response.status_code in (200, 202) for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    await service.wait(responses[0].json()["id"])
    conflict = await api.post(
        "/vm-creations",
        json={"kind": "container", "name": "別の環境"},
        headers={"Idempotency-Key": KEY},
    )
    assert conflict.status_code == 409
    assert len(docker.containers.runs) == 1


async def test_existing_synchronous_endpoint_can_use_the_same_retry_identity(api, managed):
    _, _, docker = managed
    request = {"json": {"kind": "container"}, "headers": {"Idempotency-Key": KEY}}
    first = await api.post("/vms", **request)
    second = await api.post("/vms", **request)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(docker.containers.runs) == 1


@pytest.mark.parametrize(
    "headers, body, status",
    [
        ({}, {"kind": "container"}, 422),
        ({"Idempotency-Key": "short"}, {"kind": "container"}, 422),
        ({"Idempotency-Key": KEY}, {"kind": "unsupported"}, 400),
        ({"Idempotency-Key": KEY}, {"name": "a" * 129}, 422),
    ],
)
async def test_invalid_async_requests_do_not_create(api, managed, headers, body, status):
    response = await api.post("/vm-creations", json=body, headers=headers)
    assert response.status_code == status, response.text
    assert managed[2].containers.runs == []


async def test_poll_requires_authentication_and_handles_missing_ids(api, managed):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as anon:
        assert (await anon.get("/vm-creations/" + receipt().id)).status_code == 401
    assert managed[0]._starting is None
    assert (await api.get("/vm-creations/" + receipt().id)).status_code == 404
    assert (await api.get("/vm-creations/invalid-id")).status_code == 404


async def test_corrupt_receipts_are_503_and_do_not_start_new_work(api, managed):
    service, _, docker = managed
    record = receipt()
    service.store.write(record)
    service.store._path(record.id).write_text("{")
    response = await api.get("/vm-creations/" + record.id)
    assert response.status_code == 503, response.text
    assert docker.containers.runs == []
