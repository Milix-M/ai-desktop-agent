"""Run the real guest-agent daemon in a disposable container, without KVM or a guest OS."""

import os
import tempfile
import time
from pathlib import Path

import docker
import pytest

from ai_desktop_agent.vm.qga import QgaClient, QgaError

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(
        os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real guest agent opt-in"
    ),
]


@pytest.fixture
def guest_agent():
    image = os.environ.get("QGA_SMOKE_IMAGE", "ai-desktop-agent-qga-smoke")
    client = docker.from_env(timeout=15)
    container = None
    with tempfile.TemporaryDirectory(prefix="agent-qga-") as directory:
        root = Path(directory)
        (root / "qga.conf").touch()
        try:
            container = client.containers.run(
                image,
                command=[
                    "-m",
                    "unix-listen",
                    "-p",
                    "/qga/agent.sock",
                    "-t",
                    "/qga",
                    "-f",
                    "/qga/agent.pid",
                    "--allow-rpcs=guest-sync-delimited,guest-ping,guest-get-osinfo,guest-exec,guest-exec-status",
                ],
                detach=True,
                user=f"{os.getuid()}:{os.getgid()}",
                environment={"QGA_CONF": "/qga/qga.conf"},
                network_mode="none",
                read_only=True,
                cap_drop=["ALL"],
                mem_limit="128m",
                nano_cpus=1_000_000_000,
                pids_limit=32,
                volumes={directory: {"bind": "/qga", "mode": "rw"}},
                labels={"ai-desktop-agent.test": "qga-smoke"},
            )
            socket = root / "agent.sock"
            deadline = time.monotonic() + 10
            while not socket.exists():
                container.reload()
                assert container.status == "running", container.logs().decode(errors="replace")
                assert time.monotonic() < deadline, "Guest agent socket did not appear"
                time.sleep(0.05)
            yield str(socket), root
        finally:
            if container is not None:
                container.remove(force=True)
            client.close()


def test_real_agent_handles_unicode_input_errors_and_reconnection(guest_agent):
    path, _root = guest_agent
    with QgaClient(path, timeout=2) as agent:
        assert agent.ping()
        assert agent.get_osinfo()["id"] == "ubuntu"
        text = "日本語と絵文字🙂\n"
        assert agent.run(["/bin/cat"], input_data=text.encode()) == (0, text, "")
        assert agent.run(["/bin/sh", "-c", "printf error >&2; exit 7"]) == (7, "", "error")
        with pytest.raises(QgaError):
            agent.execute("guest-no-such-command")
        assert agent.ping()
    with QgaClient(path, timeout=2) as agent:
        assert agent.ping()


def test_real_command_deadline_does_not_claim_to_kill_the_remote_process(guest_agent):
    path, root = guest_agent
    with QgaClient(path, timeout=2) as agent:
        started = time.monotonic()
        with pytest.raises(QgaError) as failure:
            agent.run(["/bin/sh", "-c", "sleep 0.35; printf finished > /qga/finished"], timeout=0.2)
        assert time.monotonic() - started < 0.45
        pid = failure.value.pid
        assert pid is not None
        agent.connect()
        deadline = time.monotonic() + 2
        while True:
            status = agent.exec_status(pid)
            if status["exited"]:
                assert status["exitcode"] == 0
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert (root / "finished").read_text() == "finished"


def test_large_output_is_reported_explicitly(guest_agent):
    path, _root = guest_agent
    with QgaClient(path, timeout=2) as agent:
        with pytest.raises(QgaError) as failure:
            agent.run(["/usr/bin/head", "-c", "20000", "/dev/zero"])
        assert len(failure.value.stdout) == 16 * 1024
        assert failure.value.stderr == ""
        assert failure.value.exitcode == 0
        assert agent.ping()


def test_shell_command_preserves_quoted_paths_unicode_and_failed_exit_status(guest_agent):
    path, root = guest_agent
    command = (
        "printf '%s' '四半期報告' > '/qga/四半期 報告.txt'; "
        "cat '/qga/四半期 報告.txt'; printf warning >&2; exit 7"
    )
    with QgaClient(path, timeout=2) as agent:
        assert agent.run(command) == (7, "四半期報告", "warning")
        assert (root / "四半期 報告.txt").read_text() == "四半期報告"
        assert agent.ping()
