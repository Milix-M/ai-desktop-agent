import asyncio
import errno
import os
import stat
import threading

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.submissions import SubmissionGoneError, key_digest, task_id_for
from tests.http import AuthenticatedClient


def test_history_delete_syncs_the_directory_after_unlink(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("saved", "保存する", "completed"))
    synced = []
    original = os.fsync

    def sync(descriptor):
        assert not (store._dir / "saved.json").exists()
        synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    assert store.delete("saved") is True
    assert synced == [True]


def test_history_delete_retries_directory_sync_after_a_partial_failure(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("saved", "保存する", "completed"))

    def fail_sync(descriptor):
        raise OSError(errno.EIO, "directory sync failed")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        for _ in range(2):
            with pytest.raises(StorageError):
                store.delete("saved")
            assert not (store._dir / "saved.json").exists()
    assert store.delete("saved") is False


def test_history_delete_cannot_unlink_before_opening_its_directory(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("saved", "保存する", "completed"))

    def fail_open(*args, **kwargs):
        raise PermissionError("directory unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", fail_open)
        with pytest.raises(StorageError):
            store.delete("saved")
    assert store.load("saved") is not None


def test_durable_history_delete_keeps_the_submission_receipt(tmp_path):
    store = TaskStore(tmp_path)
    digest, fingerprint = key_digest("same-request"), "a" * 64
    record = TaskRecord(
        task_id_for(digest),
        "保存する",
        "completed",
        request_digest=digest,
        request_fingerprint=fingerprint,
    )
    store.save(record)
    receipt = store._receipt_path(digest)
    original = receipt.read_bytes()
    assert store.delete(record.id) is True
    assert receipt.read_bytes() == original
    with pytest.raises(SubmissionGoneError):
        store.claim_submission(digest, fingerprint)


@pytest.mark.parametrize("sync_fails", [False, True])
@pytest.mark.parametrize("cancel_request", [False, True])
async def test_deletion_waits_for_sync_and_reconciles_overlapping_snapshots(
    tmp_path, monkeypatch, sync_fails, cancel_request
):
    store = TaskStore(tmp_path)
    for order, name in enumerate(("previous", "latest"), start=1):
        store.save(TaskRecord(name, "保存する", "completed", vm_id="desktop", session_order=order))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_snapshot_readers", {})
    scanned, release_scan = threading.Event(), threading.Event()
    syncing, release_sync = threading.Event(), threading.Event()
    original_scan, original_sync = store.latest_per_vm, os.fsync

    def scan():
        records = original_scan()
        if not scanned.is_set():
            scanned.set()
            assert release_scan.wait(5)
        return records

    def sync(descriptor):
        syncing.set()
        assert release_sync.wait(5)
        if sync_fails:
            raise OSError(errno.EIO, "directory sync failed")
        original_sync(descriptor)

    monkeypatch.setattr(store, "latest_per_vm", scan)
    snapshot = asyncio.create_task(server._task_snapshot())
    request = None
    try:
        assert await asyncio.to_thread(scanned.wait, 2)
        monkeypatch.setattr(os, "fsync", sync)
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            request = asyncio.create_task(api.delete("/tasks/latest"))
            assert await asyncio.to_thread(syncing.wait, 2)
            assert not (store._dir / "latest.json").exists()
            assert (await api.get("/health")).status_code == 200
            if cancel_request:
                request.cancel()
                await asyncio.sleep(0)
            assert not request.done(), "deletion returned before directory sync finished"
            release_sync.set()
            if cancel_request:
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                assert (await request).status_code == (503 if sync_fails else 200)
    finally:
        release_sync.set()
        release_scan.set()
        await asyncio.gather(snapshot, *([request] if request else []), return_exceptions=True)
    assert [task.session_id for task in snapshot.result()] == ["previous"]
    assert not server._snapshot_readers
