import asyncio
import os
import shutil
import subprocess
import threading
import time

import pytest

from ai_desktop_agent.server.android_provision import AndroidProvisioner, ProvisionTarget


def test_normal_data_writes_do_not_repeat_provisioning(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    directory = tmp_path / "android"
    directory.mkdir()
    image = directory / "android-data.img"
    mkfs = shutil.which("mkfs.ext4")
    if not mkfs:
        pytest.skip("mkfs.ext4 is needed for the real filesystem identity check")
    with image.open("wb") as file:
        file.truncate(16 * 1024 * 1024)
    subprocess.run([mkfs, "-q", "-F", str(image)], check=True, capture_output=True, timeout=10)
    inode = image.stat().st_ino
    provisioner = AndroidProvisioner(state_file=tmp_path / "state.json")
    provisioner.mark_done("android")
    with image.open("r+b") as file:
        file.seek(15 * 1024 * 1024)
        file.write(b"ordinary guest write")
    os.utime(image, ns=(image.stat().st_atime_ns, image.stat().st_mtime_ns + 1_000_000_000))
    assert not provisioner.needs("android")
    subprocess.run([mkfs, "-q", "-F", str(image)], check=True, capture_output=True, timeout=10)
    assert image.stat().st_ino == inode
    assert provisioner.needs("android")


@pytest.mark.parametrize("content", ["{broken", "[]", '{"android":[]}'])
def test_corrupt_state_is_preserved_instead_of_repeating_setup(tmp_path, content):
    from ai_desktop_agent.server.persistence import StorageError

    state = tmp_path / "state.json"
    state.write_text(content)
    provisioner = AndroidProvisioner(state_file=state)
    with pytest.raises(StorageError):
        provisioner.needs("android")
    with pytest.raises(StorageError):
        provisioner.mark_done("android", data_id="known")
    assert state.read_text() == content


def test_failed_state_write_preserves_the_previous_record(tmp_path, monkeypatch):
    from ai_desktop_agent.server.persistence import StorageError

    state = tmp_path / "state.json"
    provisioner = AndroidProvisioner(state_file=state)
    provisioner.mark_done("android", data_id="first")
    original = state.read_bytes()

    def unavailable(*_):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", unavailable)
    with pytest.raises(StorageError):
        provisioner.mark_done("android", data_id="replacement")
    assert state.read_bytes() == original


def test_busy_check_failures_prevent_provisioning(tmp_path, monkeypatch):
    def unknown(_):
        raise RuntimeError("state unavailable")

    provisioner = AndroidProvisioner(state_file=tmp_path / "state.json", is_running_check=unknown)
    monkeypatch.setattr(provisioner, "_targets", lambda: [("android", "android")])
    called = []
    monkeypatch.setattr(provisioner, "provision_vm", lambda *a, **kw: called.append(a))
    provisioner.ensure()
    assert not called


async def test_suspension_drains_only_the_target_vm_and_prevents_new_attempts(
    tmp_path, monkeypatch
):
    started, drained = threading.Event(), threading.Event()
    busy = set()
    provisioner = AndroidProvisioner(
        state_file=tmp_path / "state.json", is_running_check=busy.__contains__
    )
    monkeypatch.setattr(provisioner, "_targets", lambda: [("android", "android")])
    monkeypatch.setattr(
        provisioner, "_target", lambda vm: ProvisionTarget(vm, "stable-container", "stable")
    )
    monkeypatch.setattr(provisioner, "_matches_target", lambda *args: True)
    monkeypatch.setattr(provisioner, "_data_id", lambda _: "stable")
    calls = []

    def provision(host, *, cancelled, **kwargs):
        calls.append(host)
        started.set()
        deadline = time.monotonic() + 2
        while not cancelled() and time.monotonic() < deadline:
            drained.wait(0.01)
        if cancelled():
            drained.set()
        return False

    monkeypatch.setattr(provisioner, "provision_vm", provision)
    await provisioner.start()
    try:
        assert await asyncio.to_thread(started.wait, 1)
        async with provisioner.suspend("other"):
            await asyncio.sleep(0.02)
            assert not drained.is_set()
        async with provisioner.suspend("android"):
            assert drained.is_set()
            await asyncio.to_thread(provisioner.ensure)
            assert calls == ["android"]
            busy.add("android")
    finally:
        await provisioner.stop()


async def test_a_stale_discovery_cannot_provision_a_deleted_vm(tmp_path, monkeypatch):
    discovered, release = threading.Event(), threading.Event()
    provisioner = AndroidProvisioner(state_file=tmp_path / "state.json")

    def targets():
        discovered.set()
        assert release.wait(2)
        return [("android", "android")]

    monkeypatch.setattr(provisioner, "_targets", targets)
    monkeypatch.setattr(provisioner, "_target", lambda _: None)
    calls = []
    monkeypatch.setattr(provisioner, "provision_vm", lambda *a, **kw: calls.append(a))
    worker = asyncio.create_task(asyncio.to_thread(provisioner.ensure))
    try:
        assert await asyncio.to_thread(discovered.wait, 1)
        async with provisioner.suspend("android"):
            pass
        release.set()
        await asyncio.wait_for(worker, 1)
        assert not calls
    finally:
        release.set()
        await worker


@pytest.mark.parametrize("operation", ["stop_vm", "start_vm", "delete_vm"])
@pytest.mark.parametrize("keyword", [True, False])
async def test_environment_controls_hold_off_initialization(monkeypatch, operation, keyword):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from ai_desktop_agent.server import app as server
    from ai_desktop_agent.server.vm_pool import VmInfo

    held = set()

    @asynccontextmanager
    async def suspend(vm_id):
        held.add(vm_id)
        try:
            yield
        finally:
            held.remove(vm_id)

    def control(vm_id):
        assert held == {"and-target"}
        assert vm_id == "and-target"
        return True if operation == "delete_vm" else VmInfo(vm_id, "Android", "running")

    pool = SimpleNamespace(**{operation.replace("delete", "remove"): control})
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    monkeypatch.setattr(server, "_android_provisioner", SimpleNamespace(suspend=suspend))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    method = getattr(server, operation)
    if keyword:
        await method(vm_id="and-target")
    else:
        await method("and-target")
    assert not held


async def test_repeated_cancellation_of_stop_still_drains_the_worker(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    provisioner = AndroidProvisioner(state_file=tmp_path / "state.json")
    monkeypatch.setattr(provisioner, "_targets", lambda: [("android", "android")])
    monkeypatch.setattr(
        provisioner, "_target", lambda _: ProvisionTarget("android", "stable-container", "stable")
    )
    monkeypatch.setattr(provisioner, "_matches_target", lambda *args: True)
    monkeypatch.setattr(provisioner, "_data_id", lambda _: "stable")

    def provision(host, **kwargs):
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(provisioner, "provision_vm", provision)
    await provisioner.start()
    stopping = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        stopping = asyncio.create_task(provisioner.stop())
        await asyncio.sleep(0.01)
        for _ in range(2):
            stopping.cancel()
            await asyncio.sleep(0.01)
            assert not stopping.done()
    finally:
        release.set()
        if stopping:
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await provisioner.stop()
    assert provisioner._task is None and not provisioner._attempts
    assert provisioner.needs("android")


async def test_stop_drains_the_worker_and_does_not_mark_an_interrupted_attempt_done(
    tmp_path, monkeypatch
):
    provisioner = AndroidProvisioner(state_file=tmp_path / "state.json")
    started, release, completed = (threading.Event() for _ in range(3))
    monkeypatch.setattr(provisioner, "_targets", lambda: [("android", "android")])
    monkeypatch.setattr(
        provisioner, "_target", lambda _: ProvisionTarget("android", "stable-container", "stable")
    )
    monkeypatch.setattr(provisioner, "_matches_target", lambda *args: True)
    monkeypatch.setattr(provisioner, "_data_id", lambda _: "stable")

    def provision(host, **kwargs):
        started.set()
        assert release.wait(2)
        return True

    original = provisioner.ensure

    def ensure():
        try:
            original()
        finally:
            completed.set()

    monkeypatch.setattr(provisioner, "provision_vm", provision)
    monkeypatch.setattr(provisioner, "ensure", ensure)
    await provisioner.start()
    stopping = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        stopping = asyncio.create_task(provisioner.stop())
        await asyncio.sleep(0.03)
        assert not stopping.done()
    finally:
        release.set()
        if stopping:
            await stopping
        else:
            await provisioner.stop()
        assert await asyncio.to_thread(completed.wait, 1)
    assert provisioner.needs("android")
