"""Inspect the actual Compose desktop's Docker and kernel resource limits."""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import docker
import pytest

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


@pytest.mark.parametrize("custom", [False, True])
def test_compose_desktop_enforces_limits_and_passes_them_to_backend(tmp_path, custom):
    project = "resources-smoke-" + uuid.uuid4().hex[:10]
    image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    memory_mb, cpus, pids = (3072, 1.5, 768) if custom else (2048, 2, 1024)
    settings = {
        "DESKTOP_MEMORY_MB": str(memory_mb),
        "DESKTOP_CPUS": str(cpus),
        "DESKTOP_PIDS_LIMIT": str(pids),
        "DESKTOP_RUNTIME": "runc" if custom else "",
    }
    env = {k: v for k, v in os.environ.items() if k not in settings}
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "docker-compose.yml", tmp_path / "compose.yml"
    )
    (tmp_path / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in settings.items()) if custom else ""
    )
    (tmp_path / "image.json").write_text(json.dumps({"services": {"desktop": {"image": image}}}))
    command = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "compose.yml",
        "-f",
        "image.json",
        "--profile",
        "container",
    ]

    def compose(*args, check=True):
        return subprocess.run(
            [*command, *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=check,
        )

    client = docker.from_env(timeout=15)
    try:
        config = json.loads(compose("config", "--format", "json").stdout)
        for name, value in settings.items():
            assert config["services"]["backend"]["environment"][name] == value
        assert config["services"]["desktop"].get("runtime") == ("runc" if custom else None)
        compose("up", "-d", "--no-build", "--no-deps", "--wait", "desktop")
        [guest] = client.containers.list(
            filters={
                "label": [
                    f"com.docker.compose.project={project}",
                    "com.docker.compose.service=desktop",
                ]
            }
        )
        host = guest.attrs["HostConfig"]
        assert host["Runtime"] == (settings["DESKTOP_RUNTIME"] or client.info()["DefaultRuntime"])
        assert host["Memory"] == host["MemorySwap"] == memory_mb * 1024**2
        assert host["NanoCpus"] == cpus * 1_000_000_000
        assert host["PidsLimit"] == pids
        result = guest.exec_run(
            [
                "python3",
                "-c",
                """
import json
from pathlib import Path
root=Path('/sys/fs/cgroup')
names=['memory.max','memory.swap.max','cpu.max','pids.max']
print(json.dumps({name:(root/name).read_text().strip() for name in names}))
""",
            ]
        )
        assert result.exit_code == 0, result.output.decode()
        actual = json.loads(result.output)
        assert int(actual["memory.max"]) == memory_mb * 1024**2
        assert int(actual["memory.swap.max"]) == 0
        quota, period = map(int, actual["cpu.max"].split())
        assert quota / period == cpus
        assert int(actual["pids.max"]) == pids
    finally:
        try:
            compose("down", "--volumes", "--remove-orphans")
        finally:
            client.close()
