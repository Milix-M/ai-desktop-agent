"""Do not execute or record setup against a different Android instance."""

import asyncio
import json
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

from ai_desktop_agent.server import android_adb as adb
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    original_open = Path.open

    def open_local(path, *args, **kwargs):
        if str(path) == "/app/android-host/android-data.img":
            raise FileNotFoundError(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_local)
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    setup = AndroidProvisioner(pool=pool, state_file=tmp_path / "setup.json", apks_dir=tmp_path)

    def container(name, identity):
        target = _FakeContainer(name, labels={"com.docker.compose.service": "android"})
        target.id = identity
        client.containers.items.append(target)
        client.networks.get("ai-desktop-agent_default").connect(target)
        return target

    def disk(value=None):
        image = tmp_path / "android/android-data.img"
        image.parent.mkdir(exist_ok=True)
        block = bytearray(1024 + 0x78)
        block[1024 + 0x38 : 1024 + 0x3A] = b"\x53\xef"
        block[1024 + 0x68 : 1024 + 0x78] = uuid.UUID(
            value or "11111111-1111-1111-1111-111111111111"
        ).bytes
        image.write_bytes(block)

    return client, pool, setup, container, disk


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("change", ["promotion", "replacement"])
def test_primary_change_during_preparation_does_not_configure_a_stale_host_or_mark_done(
    environment, monkeypatch, persistent, change
):
    _, pool, setup, container, disk = environment
    if persistent:
        disk()
    first = container(
        "manual-only" if change == "promotion" else "ai-desktop-agent-android-1", "old-instance"
    )
    original = pool.prepare_vm
    changed = False

    def prepare(vm_id):
        nonlocal changed
        if not changed:
            changed = True
            if change == "replacement":
                first.remove(force=True)
            container("ai-desktop-agent-android-1", "new-instance")
        original(vm_id)

    monkeypatch.setattr(pool, "prepare_vm", prepare)
    configured = []
    monkeypatch.setattr(
        setup, "provision_vm", lambda host, **kwargs: configured.append(host) or True
    )
    setup.ensure()
    assert not configured, "a host captured before the primary change was used"
    assert setup.needs("android"), "the new primary was recorded as configured without setup"
    setup.ensure()
    assert configured == ["ai-desktop-agent-android-1"]
    assert not setup.needs("android")


def fake_adb(tmp_path, monkeypatch, *, block=False):
    log = tmp_path / "commands.jsonl"
    started = tmp_path / "started"
    executable = tmp_path / "adb-probe"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,sys,time\n"
        f"with open({str(log)!r},'a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"with open({str(started) + '.tmp'!r},'w') as output: output.write(str(os.getpid()))\n"
        f"os.replace({str(started) + '.tmp'!r},{str(started)!r})\n"
        + ("time.sleep(20)\n" if block else "")
    )
    executable.chmod(0o700)
    monkeypatch.setattr(adb, "_adb_bin", lambda: str(executable))
    return log, started


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("change", ["replacement", "shadow", "stopped", "lookup-failure"])
def test_each_adb_command_is_bound_to_the_captured_primary(
    environment, tmp_path, monkeypatch, persistent, change
):
    _, pool, setup, container, disk = environment
    if persistent:
        disk()
    primary = container("ai-desktop-agent-android-1", "old-instance")
    shadow = container("manual-shadow", "shadow-instance")
    log, _ = fake_adb(tmp_path, monkeypatch)
    monkeypatch.setattr(setup, "_wait_adb_serial", lambda host, **kwargs: "android:5555")

    def first(serial, *, runner):
        assert runner(serial, "shell", "first-step")
        if change == "replacement":
            primary.remove(force=True)
            container(primary.name, "new-instance")
        elif change == "shadow":
            shadow.remove(force=True)
            container(shadow.name, "new-shadow")
        elif change == "stopped":
            primary.status = "exited"
        else:
            from ai_desktop_agent.server.vm_control import DockerUnavailableError

            def unavailable(_):
                raise DockerUnavailableError("controlled lookup failure")

            monkeypatch.setattr(pool, "get_instance", unavailable)
        return True

    monkeypatch.setattr(adb, "setup_sleep", first)
    monkeypatch.setattr(
        adb, "setup_timezone", lambda serial, **kw: kw["runner"](serial, "shell", "second-step")
    )
    monkeypatch.setattr(
        adb, "setup_wifi", lambda serial, **kw: kw["runner"](serial, "shell", "third-step")
    )
    monkeypatch.setattr(
        adb, "setup_home", lambda serial, **kw: kw["runner"](serial, "shell", "fourth-step")
    )
    monkeypatch.setattr(adb, "dismiss_dialogs", lambda *args, **kwargs: True)
    setup.ensure()
    commands = [json.loads(line)[-1] for line in log.read_text().splitlines()]
    assert commands == (
        ["first-step"]
        if change != "shadow"
        else ["first-step", "second-step", "third-step", "fourth-step"]
    )
    assert setup.needs("android") is (change != "shadow")


def test_data_identity_change_stops_further_commands_even_with_the_same_container(
    environment, tmp_path, monkeypatch
):
    _, _, setup, container, disk = environment
    disk()
    container("ai-desktop-agent-android-1", "same-instance")
    log, _ = fake_adb(tmp_path, monkeypatch)
    monkeypatch.setattr(setup, "_wait_adb_serial", lambda host, **kwargs: "android:5555")

    def first(serial, *, runner):
        assert runner(serial, "shell", "first-step")
        disk("22222222-2222-2222-2222-222222222222")
        return True

    monkeypatch.setattr(adb, "setup_sleep", first)
    for name in ("setup_timezone", "setup_wifi", "setup_home"):
        monkeypatch.setattr(
            adb, name, lambda serial, **kw: kw["runner"](serial, "shell", "unexpected-step")
        )
    monkeypatch.setattr(adb, "dismiss_dialogs", lambda *args, **kwargs: True)
    setup.ensure()
    assert [json.loads(line)[-1] for line in log.read_text().splitlines()] == ["first-step"]
    assert setup.needs("android")


@pytest.mark.parametrize("change", ["replacement", "lookup-failure"])
async def test_primary_change_or_unknown_state_cancels_an_inflight_adb_process_and_its_attempt(
    environment, tmp_path, monkeypatch, change
):
    _, pool, setup, container, disk = environment
    disk()  # Stable data UUID must not hide a different physical container.
    primary = container("ai-desktop-agent-android-1", "old-instance")
    _, started = fake_adb(tmp_path, monkeypatch, block=True)
    monkeypatch.setattr(setup, "_wait_adb_serial", lambda host, **kwargs: "android:5555")
    monkeypatch.setattr(
        adb, "setup_sleep", lambda serial, **kw: kw["runner"](serial, "shell", "long-step")
    )
    worker = asyncio.create_task(asyncio.to_thread(setup.ensure))
    pid = None
    try:
        async with asyncio.timeout(2):
            while not started.exists():
                await asyncio.sleep(0.01)
        pid = int(started.read_text())
        if change == "replacement":
            primary.remove(force=True)
            container(primary.name, "new-instance")
        else:
            from ai_desktop_agent.server.vm_control import DockerUnavailableError

            def unavailable(_):
                raise DockerUnavailableError("controlled lookup failure")

            monkeypatch.setattr(pool, "get_instance", unavailable)
        done, _ = await asyncio.wait([worker], timeout=3)
        assert done, "an ADB process kept running after its captured primary was replaced"
        assert setup.needs("android")
    finally:
        setup._stop_requested.set()
        await worker
    if pid is not None:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_target_validation_cannot_start_a_command_after_the_attempt_deadline(tmp_path, monkeypatch):
    from ai_desktop_agent.server import adb_process

    log, _ = fake_adb(tmp_path, monkeypatch)
    now = [0.0]
    monkeypatch.setattr(adb_process.time, "monotonic", lambda: now[0])

    def check():
        now[0] = 2.0
        return True

    runner = adb_process.AdbRunner(1.0, lambda: False, before_execute=check)
    assert not runner.run("android:5555", "shell", "past-deadline")
    assert not log.exists()


async def test_a_blocked_docker_lookup_cannot_keep_the_adb_child_alive(
    environment, tmp_path, monkeypatch
):
    _, pool, setup, container, _ = environment
    container("ai-desktop-agent-android-1", "same-instance")
    _, started = fake_adb(tmp_path, monkeypatch, block=True)
    monkeypatch.setattr(setup, "_wait_adb_serial", lambda host, **kwargs: "android:5555")
    monkeypatch.setattr(
        adb, "setup_sleep", lambda serial, **kw: kw["runner"](serial, "shell", "long-step")
    )
    entered, release = threading.Event(), threading.Event()
    original = pool.get_instance

    def blocked(vm_id):
        entered.set()
        assert release.wait(10)
        return original(vm_id)

    worker = asyncio.create_task(asyncio.to_thread(setup.ensure))
    try:
        async with asyncio.timeout(2):
            while not started.exists():
                await asyncio.sleep(0.01)
        pid = int(started.read_text())
        monkeypatch.setattr(pool, "get_instance", blocked)
        assert await asyncio.to_thread(entered.wait, 2)
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("an unresponsive Docker lookup kept the ADB child alive")
        assert not worker.done(), "the in-flight lookup was abandoned before handover"
    finally:
        setup._stop_requested.set()
        release.set()
        await worker
    assert setup.needs("android")
