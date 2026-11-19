"""Observe setup ownership and cancellation through the public lifecycle methods."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server import android_provision as module
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_pool import VmInfo, VmInstance


def target_pool():
    pool = Mock()
    info = VmInfo("and-target", "saved Android", "running", kind="android", vnc_host="and-target")
    pool.list_vms.return_value = [info]
    pool.get_instance.return_value = VmInstance(info=info, container_id="stable-container")
    pool.instance_id.return_value = "stable-container"
    return pool


@pytest.mark.parametrize(
    "interval",
    [0, -1, float("nan"), float("inf"), True, None, 10**1000],
    ids=["zero", "negative", "nan", "infinite", "boolean", "null", "overflow"],
)
async def test_invalid_polling_interval_never_starts_discovery(tmp_path, interval, monkeypatch):
    monkeypatch.setenv("ANDROID_PROVISION", "1")
    pool = Mock()
    pool.list_vms.return_value = []
    setup = AndroidProvisioner(pool=pool, state_file=tmp_path / "state.json")
    try:
        with pytest.raises(ValueError):
            await setup.start(interval=interval)
        pool.list_vms.assert_not_called()
    finally:
        await setup.stop()


async def test_repeated_start_creates_one_poller_and_repeated_stop_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_PROVISION", "1")
    tasks, visited = [], threading.Event()
    create_task = asyncio.create_task

    def launch(coroutine):
        task = create_task(coroutine)
        tasks.append(task)
        return task

    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"create_task": launch}))
    )
    pool = Mock()
    pool.list_vms.side_effect = lambda: visited.set() or []
    setup = AndroidProvisioner(pool=pool, state_file=tmp_path / "state.json")
    try:
        await setup.start(interval=10)
        await setup.start(interval=10)
        assert await asyncio.to_thread(visited.wait, 1)
        assert len(tasks) == 1
    finally:
        await setup.stop()
        await setup.stop()
    assert tasks[0].done()
    pool.close.assert_not_called()


async def test_polling_retries_a_storage_failure_without_starting_setup(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("ANDROID_PROVISION", "1")
    recovered = threading.Event()
    visits = []

    def discover():
        visits.append(1)
        if len(visits) == 1:
            raise StorageError("temporary metadata read failure")
        recovered.set()
        return []

    pool = Mock()
    pool.list_vms.side_effect = discover
    setup = AndroidProvisioner(pool=pool, state_file=tmp_path / "state.json")
    try:
        await setup.start(interval=0.01)
        assert await asyncio.to_thread(recovered.wait, 1)
    finally:
        await setup.stop()
    assert len(visits) >= 2 and "巡回でエラー" in caplog.text
    pool.prepare_vm.assert_not_called()


async def test_nested_suspension_remains_active_until_the_last_owner_exits(tmp_path, monkeypatch):
    pool = target_pool()
    setup = AndroidProvisioner(pool=pool, state_file=tmp_path / "state.json")
    completed = []
    monkeypatch.setattr(
        setup, "provision_vm", lambda host, **kwargs: completed.append(host) or True
    )
    async with setup.suspend("and-target"):
        async with setup.suspend("and-target"):
            await asyncio.to_thread(setup.ensure)
        await asyncio.to_thread(setup.ensure)
        assert completed == []
        pool.prepare_vm.assert_not_called()
    await asyncio.to_thread(setup.ensure)
    assert completed == ["and-target"] and not setup.needs("and-target")


async def test_cancelled_stop_still_drains_an_explicit_ensure_attempt(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    setup = AndroidProvisioner(pool=target_pool(), state_file=tmp_path / "state.json")

    def provision(host, **kwargs):
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(setup, "provision_vm", provision)
    worker = asyncio.create_task(asyncio.to_thread(setup.ensure))
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(setup.stop())
        for _ in range(2):
            await asyncio.sleep(0.01)
            stopping.cancel()
            await asyncio.sleep(0.01)
            assert not stopping.done()
    finally:
        release.set()
        await worker
        if stopping is not None:
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await setup.stop()
    assert setup.needs("and-target")


async def test_cancelled_stop_drains_and_closes_only_an_internally_created_pool(
    tmp_path, monkeypatch
):
    from ai_desktop_agent.server import vm_pool

    entered, release = threading.Event(), threading.Event()
    pool = Mock()
    pool.list_vms.return_value = []

    def close():
        entered.set()
        assert release.wait(3)

    pool.close.side_effect = close
    monkeypatch.setattr(vm_pool, "VmPool", lambda: pool)
    setup = AndroidProvisioner(state_file=tmp_path / "state.json")
    await asyncio.to_thread(setup.ensure)
    stopping = asyncio.create_task(setup.stop())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(2):
            stopping.cancel()
            await asyncio.sleep(0.01)
            assert not stopping.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    await setup.stop()
    pool.close.assert_called_once_with()


async def test_stopped_service_does_not_reopen_a_pool_until_explicit_restart(tmp_path, monkeypatch):
    from ai_desktop_agent.server import vm_pool

    visited = threading.Event()
    pool = Mock()
    pool.list_vms.side_effect = lambda: visited.set() or []
    create = Mock(return_value=pool)
    monkeypatch.setattr(vm_pool, "VmPool", create)
    monkeypatch.setenv("ANDROID_PROVISION", "1")
    setup = AndroidProvisioner(state_file=tmp_path / "state.json")
    await setup.stop()
    await asyncio.to_thread(setup.ensure)
    create.assert_not_called()
    try:
        await setup.start(interval=10)
        assert await asyncio.to_thread(visited.wait, 1)
    finally:
        await setup.stop()
    create.assert_called_once_with()
    pool.close.assert_called_once_with()


@pytest.mark.parametrize(
    "change",
    ["gone-before", "kind-before", "stopped-before", "gone-after", "kind-after", "stopped-after"],
)
def test_target_disappearance_or_role_change_never_configures_or_records_setup(
    tmp_path, monkeypatch, change
):
    pool = target_pool()
    current = pool.get_instance.return_value
    replacement = None
    if change.startswith("kind") or change.startswith("stopped"):
        info = VmInfo(
            "and-target",
            "changed",
            "stopped" if change.startswith("stopped") else "running",
            kind="container" if change.startswith("kind") else "android",
            vnc_host="and-target",
        )
        replacement = VmInstance(info=info, container_id=current.container_id)
    pool.get_instance.side_effect = (
        [replacement] if change.endswith("before") else [current, replacement]
    )
    setup = AndroidProvisioner(pool=pool, state_file=tmp_path / "state.json")
    configured = []
    monkeypatch.setattr(
        setup, "provision_vm", lambda host, **kwargs: configured.append(host) or True
    )
    setup.ensure()
    assert configured == [] and not (tmp_path / "state.json").exists()
    assert pool.prepare_vm.call_count == (0 if change.endswith("before") else 1)


@pytest.mark.parametrize("after", ["home", "dialogs"])
def test_cancellation_between_steps_does_not_dismiss_or_install_after_stop(
    tmp_path, monkeypatch, after
):
    from ai_desktop_agent.server import android_adb as adb

    cancelled, steps, installed = threading.Event(), [], []
    (tmp_path / "saved.apk").write_bytes(b"APK fixture; installation must not run")
    setup = AndroidProvisioner(
        pool=target_pool(), state_file=tmp_path / "state.json", apks_dir=tmp_path
    )
    monkeypatch.setattr(adb, "connect", lambda *args, **kwargs: "and-target:5555")

    def step(name):
        def run(*args, **kwargs):
            steps.append(name)
            if name == after:
                cancelled.set()
            return True

        return run

    for name in ["sleep", "timezone", "wifi", "home"]:
        monkeypatch.setattr(adb, "setup_" + name, step(name))
    monkeypatch.setattr(adb, "dismiss_dialogs", step("dialogs"))
    monkeypatch.setattr(adb, "install_apk", lambda *args, **kwargs: installed.append(args) or True)
    assert setup.provision_vm("and-target", cancelled=cancelled.is_set) is False
    assert installed == []
    assert steps == ["sleep", "timezone", "wifi", "home"] + (
        ["dialogs"] if after == "dialogs" else []
    )


@pytest.mark.parametrize("outcome", ["connect", "custom-nap", "timeout", "cancel"])
def test_adb_polling_without_a_custom_nap_obeys_cancellation_and_deadline(
    tmp_path, monkeypatch, outcome
):
    from ai_desktop_agent.server import android_adb as adb

    monkeypatch.setattr(module, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(module, "ADB_WAIT_TIMEOUT", 0.05)
    cancelled, calls, steps, naps = threading.Event(), [], [], []
    successful = outcome in ("connect", "custom-nap")

    def connect(*args, **kwargs):
        calls.append(args)
        return "and-target:5555" if successful and len(calls) == 2 else None

    monkeypatch.setattr(adb, "connect", connect)
    for name in ["setup_sleep", "setup_timezone", "setup_wifi", "setup_home", "dismiss_dialogs"]:
        monkeypatch.setattr(adb, name, lambda *args, **kwargs: steps.append(args) or True)
    setup = AndroidProvisioner(
        pool=target_pool(),
        state_file=tmp_path / "state.json",
        apks_dir=tmp_path,
        nap=naps.append if outcome == "custom-nap" else None,
    )
    timer = threading.Timer(0.02, cancelled.set)
    if outcome == "cancel":
        timer.start()
    began = time.monotonic()
    try:
        assert setup.provision_vm("and-target", cancelled=cancelled.is_set) is successful
    finally:
        if outcome == "cancel":
            timer.cancel()
            timer.join()
    assert time.monotonic() - began < 1
    assert len(calls) == 2 if successful else len(calls) >= 1
    assert len(steps) == (5 if successful else 0)
    assert naps == ([0.01] if outcome == "custom-nap" else [])
