"""The production backend emits useful, redacted HTTP access logs."""

import asyncio
import json
import os
import uuid

import pytest

from ai_desktop_agent.privacy import REDACTED
from tests.integration.test_vm_creations import CreationProject

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real backend opt-in"),
]


@pytest.mark.parametrize("mode", ["health", "not-found"])
async def test_real_backend_logs_the_request_and_status_without_exposing_credentials(
    tmp_path, mode
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    project = CreationProject(runtime)
    proof = {"mode": mode}
    try:
        await project.boot()
        path = "/health" if mode == "health" else "/missing-access-log-check"
        key = "token" if mode == "health" else "model"
        value = "query-secret-" + uuid.uuid4().hex if mode == "health" else project.key
        status = 200 if mode == "health" else 404
        async with project.api() as api:
            response = await api.get(path, params={key: value})
        assert response.status_code == status
        await asyncio.to_thread(project.backend.stop, timeout=15)
        project.backend.reload()
        logs = project.backend.logs().decode()
        (tmp_path / "backend.log").write_text(logs)
        expected = f'"GET {path}?{key}={REDACTED} HTTP/1.1" {status}'
        proof.update(
            status=response.status_code,
            exit_code=project.backend.attrs["State"]["ExitCode"],
            formatted_request_present=any(
                line.startswith("INFO:") and expected in line for line in logs.splitlines()
            ),
            logging_error_absent="Logging error" not in logs,
            secrets_absent=value not in logs and project.key not in logs,
        )
    finally:
        project.close()
        (tmp_path / "access-logging-proof.json").write_text(json.dumps(proof, indent=2))
    assert proof["logging_error_absent"] and proof["formatted_request_present"], proof
    assert proof["secrets_absent"] and proof["exit_code"] == 0, proof
