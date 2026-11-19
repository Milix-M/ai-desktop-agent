"""Verify instrumentation against real Python and Docker, including exec children."""

import json
import os
import uuid

import docker
import pytest

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(
        os.environ.get("RUN_DESKTOP_SMOKE") != "1"
        or not os.environ.get("DESKTOP_CONTAINER_COVERAGE_DIR"),
        reason="container coverage opt-in",
    ),
]

PROBE = """import json,os,subprocess,sys
from coverage import Coverage
from ai_desktop_agent.privacy import REDACTED,redact
assert Coverage.current() is not None
assert redact('Bearer private-coverage-check') == 'Bearer '+REDACTED
child=subprocess.run([sys.executable,'-c',
    'from coverage import Coverage; assert Coverage.current() is not None; '
    'from ai_desktop_agent.privacy import redact; assert redact("plain") == "plain"'],
    capture_output=True,check=True)
assert child.stdout == child.stderr == b''
print(json.dumps(dict(pid=os.getpid(),child_clean=True)))
"""


def test_only_backend_processes_and_exec_children_receive_measurement(container_coverage):
    client = docker.from_env(timeout=15)
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    labels = {"ai-desktop-agent.coverage-test": uuid.uuid4().hex}
    backend = None
    try:
        backend = client.containers.run(
            image,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=["import time; time.sleep(300)"],
            detach=True,
            network_mode="none",
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "backend"},
        )
        for python in ["/app/.venv/bin/python", "python3"]:
            before = set(container_coverage.raw.glob(".coverage.*"))
            result = backend.exec_run([python, "-c", PROBE])
            assert result.exit_code == 0, result.output
            assert json.loads(result.output)["child_clean"]
            assert len(set(container_coverage.raw.glob(".coverage.*")) - before) == 2
        control = client.containers.run(
            image,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=[
                "import importlib.util,os; "
                "assert importlib.util.find_spec('coverage') is None; "
                "assert 'COVERAGE_PROCESS_START' not in os.environ; print('unchanged')"
            ],
            network_mode="none",
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "android"},
            remove=True,
        )
        assert control == b"unchanged\n"
        container_coverage.finish()
        report = json.loads((container_coverage.directory / "container-coverage.json").read_text())
        assert set(report["files"]) == {
            "src/ai_desktop_agent/" + relative for relative in container_coverage.identity["files"]
        }
        assert "src/ai_desktop_agent/agent/decision/eval.py" in report["files"]
    finally:
        try:
            for container in client.containers.list(
                all=True,
                filters={"label": "ai-desktop-agent.coverage-test=" + next(iter(labels.values()))},
            ):
                container.remove(force=True)
        finally:
            client.close()
