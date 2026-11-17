"""Exercise monitor events and replies against QEMU, without a guest OS or KVM."""

import os
import tempfile
import time
from pathlib import Path

import docker
import pytest

from ai_desktop_agent.server.vm_control import VmController
from ai_desktop_agent.vm.qmp import QmpClient

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real QEMU opt-in"),
]


def test_real_qemu_events_do_not_replace_monitor_responses(monkeypatch):
    image = os.environ.get("QMP_SMOKE_IMAGE", "ai-desktop-agent-qmp-smoke")
    client = docker.from_env(timeout=15)
    container = None
    # Keep the host's UNIX socket path below the platform's length limit.
    with tempfile.TemporaryDirectory(prefix="agent-qmp-") as directory:
        try:
            container = client.containers.run(
                image,
                entrypoint="qemu-system-x86_64",
                command=[
                    "-machine",
                    "pc,accel=tcg",
                    "-m",
                    "64",
                    "-nodefaults",
                    "-display",
                    "none",
                    "-S",
                    "-qmp",
                    "unix:/qmp/control.sock,server=on,wait=off",
                ],
                detach=True,
                user=f"{os.getuid()}:{os.getgid()}",
                network_mode="none",
                read_only=True,
                cap_drop=["ALL"],
                mem_limit="256m",
                nano_cpus=1_000_000_000,
                pids_limit=64,
                volumes={directory: {"bind": "/qmp", "mode": "rw"}},
                labels={"ai-desktop-agent.test": "qmp-smoke"},
            )
            socket = Path(directory) / "control.sock"
            deadline = time.monotonic() + 10
            while not socket.exists():
                container.reload()
                assert container.status == "running", container.logs().decode(errors="replace")
                assert time.monotonic() < deadline, "QMP socket did not appear"
                time.sleep(0.05)
            with QmpClient(str(socket), timeout=2) as qmp:
                assert qmp.query_status()["running"] is False
                assert qmp.execute("cont")["return"] == {}
                assert qmp.query_status()["running"] is True
                assert qmp.execute("stop")["return"] == {}
                assert qmp.query_status()["running"] is False
                error = qmp.execute("not-a-qmp-command")
                assert error["error"]["class"] == "CommandNotFound"
                assert qmp.query_status()["status"] == "paused"
            # Closing and opening a monitor must negotiate capabilities again.
            with QmpClient(str(socket), timeout=2) as qmp:
                assert qmp.query_status()["running"] is False
            monkeypatch.setenv("QMP_SOCK", str(socket))
            status = VmController(container_name=container.id, client=client).status()
            assert status.running and status.qmp_status == "paused"
        finally:
            if container is not None:
                container.remove(force=True)
            client.close()
