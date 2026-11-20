"""Check the evaluation CLI against real apps, without sending model requests."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import docker
import pytest

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


def test_scripted_evaluation_checks_four_fresh_desktops_and_cleans_up(tmp_path):
    image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    output = Path(os.environ.get("DESKTOP_EVAL_RESULTS_DIR", str(tmp_path))) / (
        "scripted-" + uuid.uuid4().hex[:8]
    )
    command = [
        sys.executable,
        "scripts/eval_desktop.py",
        "--mode",
        "scripted",
        "--image",
        image,
        "--out",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["mode"] == "scripted" and report["model"] == "scripted-v2"
    assert report["status"] == "completed" and report["requested_attempts"] == 4
    assert report["summary"]["successes"] == 4 and report["summary"]["infrastructure_errors"] == 0
    assert report["summary"]["model_requests"] == 0
    assert len({r["container_id"] for r in report["results"]}) == 4
    assert len({r["network_id"] for r in report["results"]}) == 4
    client = docker.from_env()
    try:
        for result in report["results"]:
            assert result["agent_success"] and result["artifact_success"]
            assert not client.containers.list(
                all=True, filters={"label": "ai-desktop-agent.evaluation=" + result["attempt_id"]}
            )
            assert not client.networks.list(
                filters={"label": "ai-desktop-agent.evaluation=" + result["attempt_id"]}
            )
    finally:
        client.close()


@pytest.mark.parametrize(
    "interrupt_signal", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"]
)
def test_interrupting_evaluation_records_the_stop_and_removes_owned_resources(
    tmp_path, interrupt_signal
):
    image = os.environ.get("DESKTOP_SMOKE_IMAGE", "ai-desktop-agent-desktop-smoke")
    output = Path(os.environ.get("DESKTOP_EVAL_RESULTS_DIR", str(tmp_path))) / (
        "interrupted-" + uuid.uuid4().hex[:8]
    )
    command = [
        sys.executable,
        "scripts/eval_desktop.py",
        "--mode",
        "scripted",
        "--image",
        image,
        "--cases",
        "spreadsheet-total",
        "--out",
        str(output),
    ]
    client = docker.from_env()
    attempt_id = None
    log_path = tmp_path / "process.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()
                if (output / "report.json").exists():
                    report = json.loads((output / "report.json").read_text())
                    if report["results"]:
                        attempt_id = report["results"][0]["attempt_id"]
                        if client.containers.list(
                            all=True,
                            filters={"label": "ai-desktop-agent.evaluation=" + attempt_id},
                        ):
                            break
                time.sleep(0.05)
            else:
                pytest.fail("Evaluation did not allocate a desktop: " + log_path.read_text())
            process.send_signal(interrupt_signal)
            assert process.wait(timeout=30) == 130, log_path.read_text()
            report = json.loads((output / "report.json").read_text())
            assert report["status"] == "interrupted"
            assert report["results"][0]["status"] == "interrupted"
            assert not client.containers.list(
                all=True, filters={"label": "ai-desktop-agent.evaluation=" + attempt_id}
            )
            assert not client.networks.list(
                filters={"label": "ai-desktop-agent.evaluation=" + attempt_id}
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
            if attempt_id:
                for container in client.containers.list(
                    all=True, filters={"label": "ai-desktop-agent.evaluation=" + attempt_id}
                ):
                    container.remove(force=True)
                for network in client.networks.list(
                    filters={"label": "ai-desktop-agent.evaluation=" + attempt_id}
                ):
                    network.remove()
            client.close()
