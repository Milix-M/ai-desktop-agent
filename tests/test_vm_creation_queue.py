"""Queued creations must not consume the workers used for state queries."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_creations import CreationStore, VmCreationService
from ai_desktop_agent.vm import async_io
from tests.http import AuthenticatedClient
from tests.test_vm_concurrency import slow_creation as slow_creation


@pytest.fixture
async def service(slow_creation, tmp_path, monkeypatch):
    pool = slow_creation[0]
    service = VmCreationService(pool, CreationStore(tmp_path / "requests"))
    monkeypatch.setattr(server, "_vm_creations", service)
    yield service
    await service.close()


async def test_waiting_creators_do_not_starve_a_small_shared_executor(slow_creation):
    _, _, entered, release, _ = slow_creation
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    creations = []
    probe = None
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            creations = [
                asyncio.create_task(api.post("/vms", json={"kind": "qemu"})) for _ in range(3)
            ]
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            probe = asyncio.create_task(api.get("/vm/status"))
            _, pending = await asyncio.wait([probe], timeout=1)
            assert not pending, "waiting creations exhausted the state-query executor"
            assert probe.result().status_code == 200
            assert not any(task.done() for task in creations)
        finally:
            release.set()
            results = await asyncio.gather(*creations)
            if probe is not None:
                await probe
    assert all(result.status_code == 200 for result in results)


async def test_creation_backlog_is_bounded_and_releases_capacity(slow_creation):
    _, _, entered, release, _ = slow_creation
    creations = []
    rejected = None
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            creations = [
                asyncio.create_task(api.post("/vms", json={"kind": "qemu"})) for _ in range(8)
            ]
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            rejected = asyncio.create_task(api.post("/vms", json={"kind": "qemu"}))
            _, pending = await asyncio.wait([rejected], timeout=1)
            assert not pending, "a full creation backlog accepted another waiting request"
            assert rejected.result().status_code == 429, rejected.result().text
            assert rejected.result().headers["Retry-After"]
            assert (await api.get("/vm/status")).status_code == 200
        finally:
            release.set()
            results = await asyncio.gather(*creations)
            if rejected is not None:
                await rejected
        assert all(result.status_code == 200 for result in results)
        assert (await api.delete("/vms/" + results[0].json()["id"])).status_code == 200
        assert (await api.post("/vms", json={"kind": "qemu"})).status_code == 200


async def test_cancelled_creation_retains_its_slot_until_the_worker_finishes(
    slow_creation, monkeypatch
):
    _, _, entered, release, _ = slow_creation
    monkeypatch.setattr(async_io, "_VM_CREATION_SLOTS", threading.BoundedSemaphore(1))
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        creation = asyncio.create_task(api.post("/vms", json={"kind": "qemu"}))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            creation.cancel()
            await asyncio.sleep(0.05)
            assert not creation.done()
            busy = await api.post("/vms", json={"kind": "qemu"})
            assert busy.status_code == 429
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await creation
        assert (await api.post("/vms", json={"kind": "qemu"})).status_code == 200


async def test_a_failed_creation_does_not_consume_its_slot(slow_creation, monkeypatch):
    pool, _, _, release, _ = slow_creation
    monkeypatch.setattr(async_io, "_VM_CREATION_SLOTS", threading.BoundedSemaphore(1))
    original = pool._create_overlay

    def fail(_vm_id):
        raise DockerUnavailableError("controlled copy failure")

    monkeypatch.setattr(pool, "_create_overlay", fail)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        failed = await api.post("/vms", json={"kind": "qemu"})
        assert failed.status_code == 503
        monkeypatch.setattr(pool, "_create_overlay", original)
        release.set()
        assert (await api.post("/vms", json={"kind": "qemu"})).status_code == 200


async def test_async_receipts_and_legacy_requests_share_one_backlog(slow_creation, service):
    _, _, entered, release, _ = slow_creation
    extra = None
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            first = await api.post(
                "/vm-creations",
                json={"kind": "qemu"},
                headers={"Idempotency-Key": "mixed-backlog-first-0123456789"},
            )
            assert first.status_code == 202, first.text
            assert await asyncio.to_thread(entered.wait, 2)
            for i in range(7):
                response = await api.post(
                    "/vm-creations",
                    json={"kind": "container"},
                    headers={"Idempotency-Key": f"mixed-backlog-extra-{i}-0123456789"},
                )
                assert response.status_code == 202, response.text
            extra = asyncio.create_task(api.post("/vms", json={"kind": "container"}))
            _, pending = await asyncio.wait([extra], timeout=1)
            assert not pending, "the legacy API bypassed the accepted-job backlog"
            assert extra.result().status_code == 429
        finally:
            release.set()
            if extra is not None:
                await extra


async def test_a_full_legacy_backlog_rejects_new_receipts_before_persisting(slow_creation, service):
    _, _, entered, release, _ = slow_creation
    requests = []
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        try:
            requests = [
                asyncio.create_task(api.post("/vms", json={"kind": "qemu"})) for _ in range(9)
            ]
            assert await asyncio.to_thread(entered.wait, 2)
            done, _ = await asyncio.wait(requests, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert done and all(task.result().status_code == 429 for task in done)
            response = await api.post(
                "/vm-creations",
                json={"kind": "container"},
                headers={"Idempotency-Key": "full-legacy-receipt-0123456789"},
            )
            assert response.status_code == 429, response.text
            assert not list(service.store.directory.glob("creation-*.json"))
        finally:
            release.set()
            await asyncio.gather(*requests)
