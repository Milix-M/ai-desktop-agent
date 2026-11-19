"""Evaluation infrastructure failures must not become model results or green exits."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from docker.errors import NotFound

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.evaluation import desktop
from ai_desktop_agent.evaluation.cases import CASES
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_desktop_evaluation import archive_file
from tests.test_desktop_evaluation import environment as environment


def arguments(output, **changes):
    return SimpleNamespace(
        **(
            {
                "mode": "model",
                "provider": "fixture-provider",
                "model": None,
                "base_url": None,
                "image": "fixture-image",
                "cases": [CASES[0].id],
                "repeats": 1,
                "out": str(output),
                "max_seconds": None,
                "max_requests": None,
                "max_tokens": None,
            }
            | changes
        )
    )


@pytest.mark.parametrize("fault", ["missing", "wrong-content", "wrong-control", "stale-target"])
async def test_unprepared_input_never_reaches_the_model(environment, tmp_path, monkeypatch, fault):
    client, container = environment
    case = CASES[1] if fault in {"wrong-control", "stale-target"} else CASES[0]
    expected = {case.filename: case.initial.encode()}
    if case.renamed:
        expected["変更しない.txt"] = b"keep this file\n"
    actual = dict(expected)
    if fault == "missing":
        actual.pop(case.filename)
    elif fault == "wrong-content":
        actual[case.filename] = b"unrelated file"
    elif fault == "wrong-control":
        actual["変更しない.txt"] = b"unrelated file"
    else:
        actual[case.renamed] = b"stale target"

    def read(path):
        if Path(path).name not in actual:
            raise NotFound("missing file")
        return iter([archive_file(actual[Path(path).name])]), {}

    container.get_archive.side_effect = read
    monkeypatch.setattr(desktop, "prepare_case", lambda *args: expected)
    model = MockLLMProvider()
    model.understand_instruction = AsyncMock(wraps=model.understand_instruction)
    model.aclose = AsyncMock()
    result = {"attempt_id": "bad-input", "status": "running"}
    await desktop.run_attempt(client, "image", case, model, ExecutionLimits(), tmp_path, result)
    assert result["status"] == "setup_error"
    assert "初期" in result["errors"][0]
    assert "agent_success" not in result
    model.understand_instruction.assert_not_awaited()
    model.aclose.assert_awaited_once()
    container.remove.assert_called_once_with(force=True)
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "setup_error"


async def test_failed_final_evidence_marks_the_suite_incomplete_even_when_the_task_passes(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    case = CASES[0]
    content = case.initial.encode()

    class FailingFinalConnection(FakeDisplayBackend):
        def connect(self, *args, **kwargs):
            if self.connections:
                raise RuntimeError("final screen failed")
            return super().connect(*args, **kwargs)

        def type_text(self, text):
            nonlocal content
            content = text.encode()
            super().type_text(text)

    container.get_archive.side_effect = lambda _: (iter([archive_file(content)]), {})
    client.images.get.return_value = SimpleNamespace(id="image-sha")
    monkeypatch.setattr(desktop, "VNCClient", FailingFinalConnection)
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    code = await desktop.run_suite(arguments(output, mode="scripted", provider=None))
    report = json.loads((output / "report.json").read_text())
    assert report["results"][0]["success"] is True
    assert report["summary"]["successes"] == report["summary"]["artifact_successes"] == 1
    assert report["summary"]["infrastructure_errors"] == 1
    assert code == 2 and report["status"] == "incomplete"
    assert "final screen failed" in report["results"][0]["errors"][0]
    container.remove.assert_called_once_with(force=True)


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancellation_during_docker_client_creation_closes_the_completed_client(
    tmp_path, monkeypatch, cancellations
):
    entered, release = threading.Event(), threading.Event()
    client = Mock()
    model = MockLLMProvider()
    model.aclose = AsyncMock()

    def connect(**kwargs):
        entered.set()
        assert release.wait(3)
        return client

    monkeypatch.setattr(desktop, "create_llm_provider", Mock(return_value=model))
    monkeypatch.setattr(desktop.docker, "from_env", connect)
    output = tmp_path / "suite"
    task = asyncio.create_task(desktop.run_suite(arguments(output)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        client.close.assert_called_once()
        client.images.get.assert_not_called()
        model.aclose.assert_awaited_once()
        report = json.loads((output / "report.json").read_text())
        assert report["status"] == "interrupted" and not report["results"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("resource", ["container", "network"])
@pytest.mark.parametrize(
    "failure", [RuntimeError("daemon unavailable"), NotFound("already removed")]
)
async def test_cleanup_attempts_every_owned_resource_despite_a_prior_deletion_failure(
    environment, tmp_path, resource, failure
):
    client, container = environment
    first, second = Mock(id="first-owned"), Mock(id="second-owned")
    first.remove.side_effect = failure
    if resource == "container":
        client.containers.run.side_effect = RuntimeError("create response lost")
        client.containers.list.return_value = [first, second]
    else:
        client.networks.create.side_effect = RuntimeError("create response lost")
        client.networks.list.return_value = [first, second]
        client.containers.list.return_value = []
    result = {"attempt_id": "lost-allocation", "status": "running"}
    await desktop.run_attempt(
        client, "image", CASES[0], MockLLMProvider(), ExecutionLimits(), tmp_path, result
    )
    second.remove.assert_called_once_with(**({"force": True} if resource == "container" else {}))
    if isinstance(failure, NotFound):
        assert not result.get("cleanup_errors")
    else:
        assert "daemon unavailable" in result["cleanup_errors"][0]
