"""Verify an actual guest desktop and its retained marker through guest services and VNC."""

import subprocess
import sys
import time
from pathlib import Path

from ai_desktop_agent.server import android_adb
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.vm.qga import QgaClient
from ai_desktop_agent.vm.qmp import QmpClient
from ai_desktop_agent.vm.vnc_client import VNCClient

MARKER = "desktop-agent-persistence"


def check_linux(directory: str, phase: str):
    qmp = QmpClient(str(Path(directory) / "sockets/qmp.sock"))
    qmp.connect()
    try:
        assert qmp.execute("query-kvm")["return"]["enabled"]
    finally:
        qmp.close()
    guest = QgaClient(str(Path(directory) / "sockets/qga.sock"), timeout=5)
    guest.connect()
    try:
        assert guest.get_osinfo()["id"] == "ubuntu"
        code, output, error = guest.run(
            "pgrep -u agent -x plasmashell >/dev/null && runuser -u agent -- firefox --version"
        )
        assert code == 0 and "Firefox" in output, (code, output, error)
        marker = "/home/agent/kvm-smoke.txt"
        command = f"echo {MARKER} > {marker} && sync" if phase == "write" else f"cat {marker}"
        code, output, error = guest.run(
            ["/usr/sbin/runuser", "-u", "agent", "--", "sh", "-c", command]
        )
        assert code == 0 and (phase == "write" or output.strip() == MARKER), (code, output, error)
    finally:
        guest.close()


def adb(*args):
    return subprocess.check_output(["adb", *args], text=True, timeout=15).strip()


def check_android(address: str, phase: str):
    serial = f"{address}:5555"
    adb("connect", serial)
    assert adb("-s", serial, "shell", "getprop", "sys.boot_completed") == "1"
    assert adb("-s", serial, "shell", "getprop", "ro.build.version.sdk") == "28"
    if phase == "write":
        assert AndroidProvisioner().provision_vm(address), "Android initialization failed"
    assert (
        adb("-s", serial, "shell", "getprop", "persist.sys.timezone")
        == android_adb.DEFAULT_TIMEZONE
    ), "Android time zone was not retained"
    assert adb("-s", serial, "shell", "settings", "get", "system", "screen_off_timeout") == str(
        android_adb.SCREEN_OFF_TIMEOUT_MAX_MS
    ), "Android screen timeout was not retained"
    adb("-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME")
    activity = adb("-s", serial, "shell", "dumpsys", "activity", "activities")
    assert any(
        "mResumedActivity" in line and "launcher" in line.lower() for line in activity.splitlines()
    )
    marker = "/data/local/tmp/kvm-smoke.txt"
    if phase == "write":
        adb("-s", serial, "shell", f"echo {MARKER} > {marker} && sync")
    else:
        assert adb("-s", serial, "shell", "cat", marker) == MARKER


def check_frame(host: str, port: int):
    client = VNCClient()
    client.connect(host, port)
    try:
        frame = client.capture_raw().to_pil()
        assert frame.width >= 640 and frame.height >= 400
        assert len(set(frame.convert("RGB").resize((64, 64)).get_flattened_data())) > 8, (
            "blank VNC frame"
        )
    finally:
        client.disconnect()


if __name__ == "__main__":
    kind, phase, target, port = sys.argv[1:]
    deadline = time.monotonic() + 480
    while True:
        try:
            if kind == "linux":
                check_linux(target, phase)
            else:
                check_android(target, phase)
            check_frame("127.0.0.1" if kind == "linux" else target, int(port))
        except (AssertionError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            if time.monotonic() >= deadline:
                raise
            print(f"Waiting for {kind} guest: {error}", flush=True)
            time.sleep(3)
        else:
            print(f"{kind} guest: desktop, VNC frame and persistence {phase} passed")
            break
