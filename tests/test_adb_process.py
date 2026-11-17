"""Run disposable subprocesses instead of contacting real Android devices."""

import asyncio
import json
import os
import signal
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from ai_desktop_agent.server import android_adb
from ai_desktop_agent.server.adb_process import AdbRunner


@pytest.fixture
def adb_script(tmp_path, monkeypatch):
    def create(body):
        executable = tmp_path / "adb"
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)
        monkeypatch.setattr(android_adb, "_adb_bin", lambda: str(executable))
        return executable

    return create


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


async def wait_file(path):
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("subprocess did not start")


def test_connect_and_query_use_the_correct_argv(adb_script, tmp_path):
    log = tmp_path / "args.jsonl"
    adb_script(
        "import json,sys\n"
        f"with open({str(log)!r},'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "print('connected to android:5555' if sys.argv[1]=='connect' else '日本語の結果')\n"
    )
    runner = AdbRunner(time.monotonic() + 5, lambda: False)
    assert runner.connect("android:5555")
    assert runner.query("android:5555", "shell", "echo", "hello") == "日本語の結果"
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ["connect", "android:5555"],
        ["-s", "android:5555", "shell", "echo", "hello"],
    ]


@pytest.mark.parametrize("output", ["unable to connect", "connected to android:5555"])
def test_failed_adb_exit_is_not_a_success(adb_script, output):
    adb_script(f"import sys\nprint({output!r})\nsys.exit(1)\n")
    runner = AdbRunner(time.monotonic() + 5, lambda: False)
    assert not runner.connect("android:5555")
    assert not runner.run("android:5555", "shell", "true")
    assert runner.query("android:5555", "shell", "true") is None


async def test_cancellation_kills_and_reaps_the_command_group(adb_script, tmp_path):
    ready = tmp_path / "ready.json"
    adb_script(
        "import json,os,signal,subprocess,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])\n"
        f"with open({str(ready) + '.tmp'!r},'w') as f:json.dump([os.getpid(),child.pid],f)\n"
        f"os.replace({str(ready) + '.tmp'!r},{str(ready)!r})\n"
        "time.sleep(60)\n"
    )
    cancelled = threading.Event()
    runner = AdbRunner(time.monotonic() + 10, cancelled.is_set)
    task = asyncio.create_task(asyncio.to_thread(runner.run, "android:5555", "block"))
    pids = []
    try:
        await wait_file(ready)
        pids = json.loads(ready.read_text())
        cancelled.set()
        assert await asyncio.wait_for(asyncio.shield(task), 2) is False
        # SIGKILL and pipe EOF can precede /proc reporting the child's exit.
        # Require the whole group to stop within a bound, without a fixed sleep.
        async with asyncio.timeout(1):
            while any(running(pid) for pid in pids):
                await asyncio.sleep(0.005)
    finally:
        cancelled.set()
        if pids:
            with suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        await asyncio.wait_for(asyncio.shield(task), 2)


@pytest.mark.parametrize("limit", ["attempt", "command"])
def test_deadline_ends_a_blocking_command(adb_script, limit):
    adb_script("import time\ntime.sleep(60)\n")
    runner = AdbRunner(time.monotonic() + (0.1 if limit == "attempt" else 5), lambda: False)
    started = time.monotonic()
    assert runner.query("android:5555", "block", timeout=0.1) is None
    assert time.monotonic() - started < 1


def test_no_command_starts_after_cancellation(adb_script, tmp_path):
    touched = tmp_path / "executed"
    adb_script(f"from pathlib import Path\nPath({str(touched)!r}).touch()\n")
    runner = AdbRunner(time.monotonic() + 5, lambda: True)
    assert not runner.run("android:5555", "shell", "true")
    assert not touched.exists()


async def test_task_submission_drains_initialization_before_connecting_to_the_vm(
    adb_script, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from ai_desktop_agent.agent.llm.mock import MockLLMProvider
    from ai_desktop_agent.server import app as server
    from ai_desktop_agent.server.android_provision import AndroidProvisioner, ProvisionTarget
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.server.store import TaskStore
    from ai_desktop_agent.vm.fake import FakeDisplayBackend

    ready = tmp_path / "ready"
    adb_script(
        "import os,sys,time\n"
        "if sys.argv[1]=='connect':print('connected to android:5555');sys.exit(0)\n"
        f"with open({str(ready) + '.tmp'!r},'w') as f:f.write(str(os.getpid()))\n"
        f"os.replace({str(ready) + '.tmp'!r},{str(ready)!r})\n"
        "time.sleep(60)\n"
    )

    class WaitingLLM(MockLLMProvider):
        async def understand_instruction(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    def connect(_vm):
        assert not running(int(ready.read_text()))
        return FakeDisplayBackend()

    store = TaskStore(tmp_path / "store")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "get_store", lambda: store)
    monkeypatch.setattr(server, "_resolve_vm", lambda _: SimpleNamespace(id="android"))
    monkeypatch.setattr(server, "_default_vm_id", lambda: "android")
    monkeypatch.setattr(server, "_connect_vm_display", connect)
    monkeypatch.setattr(
        server,
        "_create_session",
        lambda display: TaskSession(llm=WaitingLLM(), display=display, store=store),
    )
    provisioner = AndroidProvisioner(
        state_file=tmp_path / "state.json",
        is_running_check=lambda vm: any(
            s.vm_id == vm and s.is_running for s in server._sessions.values()
        ),
    )
    monkeypatch.setattr(provisioner, "_targets", lambda: [("android", "android")])
    monkeypatch.setattr(
        provisioner, "_target", lambda _: ProvisionTarget("android", "stable-container", "stable")
    )
    monkeypatch.setattr(provisioner, "_matches_target", lambda *args: True)
    monkeypatch.setattr(provisioner, "_data_id", lambda _: "stable")
    monkeypatch.setattr(server, "_android_provisioner", provisioner)
    await provisioner.start()
    try:
        await wait_file(ready)
        result = await server.create_task(server.CreateTaskRequest(instruction="画面を確認する"))
        assert result.is_running and result.vm_id == "android"
        await asyncio.to_thread(provisioner.ensure)
        assert provisioner.needs("android")
    finally:
        await provisioner.stop()
        sessions = list(server._sessions.values())
        for session in sessions:
            session.stop()
        await asyncio.gather(*(s._task for s in sessions if s._task), return_exceptions=True)
        await asyncio.gather(*(s.aclose() for s in sessions))
