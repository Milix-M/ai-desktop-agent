"""Descriptor ownership survives stream setup, locking and caller failures."""

import errno
import os
import subprocess
import sys
from contextlib import suppress
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import persistence as module
from ai_desktop_agent.server.persistence import (
    StorageError,
    atomic_json,
    atomic_text,
    metadata_lock,
)
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_pool import VmInfo
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


def descriptor_identity(descriptor):
    value = os.fstat(descriptor)
    return value.st_dev, value.st_ino


def still_owned(descriptor, identity):
    try:
        return descriptor_identity(descriptor) == identity
    except OSError as error:
        assert error.errno == errno.EBADF
        return False


def refers_to(descriptor, path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return False
    return descriptor_identity(descriptor) == (value.st_dev, value.st_ino)


def reacquire_from_another_process(path):
    code = """import fcntl,sys
with open(sys.argv[1]+'.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
print('reclaimed')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "reclaimed\n"


@pytest.mark.parametrize("operation", ["lock", "json", "text"])
@pytest.mark.parametrize("failure", ["io", "interrupt"])
def test_stream_initialization_failure_closes_the_owned_descriptor_and_preserves_data(
    tmp_path, monkeypatch, operation, failure
):
    path = tmp_path / "record.json"
    atomic_text(path, "original record")
    descriptors = []
    fdopen = module.os.fdopen
    error = (
        OSError(errno.EIO, "stream initialization failed")
        if failure == "io"
        else KeyboardInterrupt()
    )

    def unavailable(descriptor, *args, **kwargs):
        if refers_to(descriptor, path.with_suffix(path.suffix + ".lock")) or any(
            refers_to(descriptor, target) for target in tmp_path.glob(f".{path.name}.*.tmp")
        ):
            descriptors.append((descriptor, descriptor_identity(descriptor)))
            raise error
        return fdopen(descriptor, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "fdopen", unavailable)
            with pytest.raises(StorageError if failure == "io" else KeyboardInterrupt) as caught:
                if operation == "lock":
                    with metadata_lock(path):
                        pytest.fail("lock body ran without a stream")
                elif operation == "json":
                    atomic_json(path, {"changed": True})
                else:
                    atomic_text(path, "changed")
        assert len(descriptors) == 1
        assert not still_owned(*descriptors[0])
        assert (caught.value.__cause__ if failure == "io" else caught.value) is error
        assert path.read_text() == "original record"
        assert not list(tmp_path.glob("*.tmp"))
        with metadata_lock(path):
            atomic_text(path, "recovered")
        assert path.read_text() == "recovered"
        reacquire_from_another_process(path)
    finally:
        for descriptor, identity in descriptors:
            if still_owned(descriptor, identity):
                with suppress(OSError):
                    os.close(descriptor)


@pytest.mark.parametrize("operation", ["json", "text"])
def test_unusable_parent_is_preserved_and_a_save_can_be_retried(tmp_path, operation):
    parent = tmp_path / "blocked"
    parent.write_text("existing user file")
    path = parent / "record.json"
    write = atomic_json if operation == "json" else atomic_text
    value = {"recovered": True} if operation == "json" else "recovered"
    with pytest.raises(StorageError) as caught:
        write(path, value)
    assert isinstance(caught.value.__cause__, FileExistsError)
    assert parent.is_file() and parent.read_text() == "existing user file"
    saved = tmp_path / "saved-parent-file"
    parent.rename(saved)
    write(path, value)
    assert path.read_text() == ('{"recovered": true}' if operation == "json" else "recovered")
    assert saved.read_text() == "existing user file"
    assert not list(parent.glob("*.tmp"))


def test_a_directory_at_the_lock_path_is_reported_without_entering_or_repairing_it(tmp_path):
    path = tmp_path / "record.json"
    atomic_text(path, "saved")
    lock = tmp_path / "record.json.lock"
    lock.mkdir()
    with pytest.raises(StorageError) as caught, metadata_lock(path):
        pytest.fail("body entered with an unusable lock path")
    assert isinstance(caught.value.__cause__, IsADirectoryError)
    assert path.read_text() == "saved" and lock.is_dir()
    lock.rmdir()
    with metadata_lock(path):
        pass
    reacquire_from_another_process(path)


@pytest.mark.parametrize("stage", ["acquire", "release"])
def test_flock_failure_releases_the_kernel_lock_and_descriptor(tmp_path, monkeypatch, stage):
    path = tmp_path / "record.json"
    atomic_text(path, "saved")
    flock = module.fcntl.flock
    descriptors = []

    def unavailable(lock, operation):
        if operation == (module.fcntl.LOCK_EX if stage == "acquire" else module.fcntl.LOCK_UN):
            descriptors.append((lock.fileno(), descriptor_identity(lock.fileno())))
            raise OSError(errno.EIO, "controlled flock failure")
        return flock(lock, operation)

    with monkeypatch.context() as patch:
        patch.setattr(module.fcntl, "flock", unavailable)
        if stage == "acquire":
            with pytest.raises(StorageError), metadata_lock(path):
                pytest.fail("body entered after flock failure")
        else:
            with metadata_lock(path):
                assert path.read_text() == "saved"
    assert len(descriptors) == 1 and not still_owned(*descriptors[0])
    assert path.read_text() == "saved"
    reacquire_from_another_process(path)


@pytest.mark.parametrize(
    "failure",
    [OSError(errno.EIO, "caller IO failure"), RuntimeError("caller failure"), KeyboardInterrupt()],
)
def test_lock_body_failure_propagates_unchanged_and_another_process_can_acquire(tmp_path, failure):
    path = tmp_path / "record.json"
    atomic_text(path, "saved")
    with pytest.raises(type(failure)) as caught, metadata_lock(path):
        raise failure
    assert caught.value is failure
    assert path.read_text() == "saved"
    reacquire_from_another_process(path)


async def test_task_api_reports_a_stream_lock_failure_without_starting_or_leaking_a_session(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("kept", "saved report", "completed"))
    sessions, descriptors = [], []
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "_resolve_vm", lambda _: VmInfo("desktop", "desktop", "running"))
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: FakeDisplayBackend())

    def create(display):
        model = MockLLMProvider()
        model.aclose = AsyncMock()
        session = TaskSession(llm=model, display=display, store=store, decision=None)
        sessions.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    fdopen = module.os.fdopen

    def unavailable(descriptor, *args, **kwargs):
        if refers_to(descriptor, tmp_path / "task-order.json.lock"):
            descriptors.append((descriptor, descriptor_identity(descriptor)))
            raise OSError(errno.EIO, "controlled task-order stream failure")
        return fdopen(descriptor, *args, **kwargs)

    try:
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as api:
            with monkeypatch.context() as patch:
                patch.setattr(module.os, "fdopen", unavailable)
                response = await api.post(
                    "/tasks", json={"instruction": "inspect report", "vm_id": "desktop"}
                )
        assert response.status_code == 503, response.text
        assert "ロックできません" in response.json()["detail"]
        assert len(descriptors) == 1 and not still_owned(*descriptors[0])
        assert len(sessions) == 1 and not sessions[0].display.is_connected
        sessions[0].llm.aclose.assert_awaited_once()
        assert sessions[0].llm.decide_call_count == 0
        assert not server._sessions and server._active_session is None
        assert [record.id for record in store.list()] == ["kept"]
    finally:
        for descriptor, identity in descriptors:
            if still_owned(descriptor, identity):
                with suppress(OSError):
                    os.close(descriptor)
