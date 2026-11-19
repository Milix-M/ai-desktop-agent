"""Keep partial evaluation reports, resource ownership and CLI failures trustworthy."""

import asyncio
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.evaluation import desktop
from ai_desktop_agent.evaluation.cases import CASES
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_desktop_evaluation import archive_file
from tests.test_desktop_evaluation import environment as environment
from tests.test_evaluation_failures import arguments


@pytest.mark.parametrize(
    "options", [["--repeats", "0"], ["--provider", "mock"], ["--max-seconds", "0"]]
)
def test_invalid_evaluation_settings_stop_before_allocating_or_creating_output(
    tmp_path, monkeypatch, capsys, options
):
    allocate = Mock(side_effect=AssertionError("must not allocate"))
    shutdown = Mock()
    monkeypatch.setattr(desktop.docker, "from_env", allocate)
    monkeypatch.setattr(desktop.VNCClient, "shutdown_runtime", shutdown)
    output = tmp_path / "suite"
    assert desktop.main([*options, "--out", str(output)]) == 2
    assert capsys.readouterr().err
    allocate.assert_not_called()
    shutdown.assert_called_once()
    assert not output.exists()


def test_the_module_cli_reports_invalid_model_mode_without_creating_a_desktop(tmp_path):
    output = tmp_path / "suite"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_desktop_agent.evaluation.desktop",
            "--mode",
            "model",
            "--provider",
            "mock",
            "--out",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2 and "mock" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "failure", [OSError("git missing"), subprocess.TimeoutExpired("git", 5), None]
)
def test_source_identity_is_preserved_without_a_git_repository(monkeypatch, failure):
    command = (
        Mock(side_effect=failure) if failure else Mock(return_value=SimpleNamespace(returncode=128))
    )
    monkeypatch.setattr(desktop.subprocess, "run", command)
    source = desktop.source_metadata()
    assert source["revision"] is None and len(source["source_sha256"]) == 64
    assert set(source["dependencies"]) == {"openai", "docker", "vncdotool", "Pillow"}
    assert all(source["dependencies"].values())


async def test_synchronous_provider_cleanup_is_also_supported():
    provider = SimpleNamespace(aclose=Mock())
    await desktop.close_provider(provider)
    provider.aclose.assert_called_once()


async def test_cancelling_an_individual_cleanup_operation_does_not_skip_the_next_one():
    result = {}
    first = AsyncMock(side_effect=asyncio.CancelledError("cleanup interrupted"))
    second = AsyncMock()
    assert await desktop.cleanup_all([("first", first), ("second", second)], result) is False
    second.assert_awaited_once()
    assert "cleanup interrupted" in result["cleanup_errors"][0]


async def test_an_already_completed_seed_is_not_a_model_success(environment, tmp_path, monkeypatch):
    client, container = environment
    case = dataclasses.replace(CASES[0], initial=CASES[0].expected)
    monkeypatch.setattr(
        desktop, "prepare_case", lambda *args: {case.filename: case.initial.encode()}
    )
    container.get_archive.side_effect = lambda _: (iter([archive_file(case.initial.encode())]), {})
    model = MockLLMProvider()
    model.understand_instruction = AsyncMock()
    result = {"attempt_id": "already-complete", "status": "running"}
    await desktop.run_attempt(client, "image", case, model, ExecutionLimits(), tmp_path, result)
    model.understand_instruction.assert_not_awaited()
    assert result["status"] == "setup_error" and "操作前" in result["errors"][0]


async def test_a_task_deadline_is_counted_as_model_failure_instead_of_an_infrastructure_error(
    environment, tmp_path
):
    client, container = environment
    model = MockLLMProvider()

    async def understand(*args, **kwargs):
        await asyncio.Event().wait()

    model.understand_instruction = understand
    result = {"attempt_id": "deadline", "status": "running"}
    await desktop.run_attempt(
        client, "image", CASES[0], model, ExecutionLimits(max_seconds=0.03), tmp_path, result
    )
    assert result["status"] == "evaluated" and not result["success"]
    assert result["stop_reason"] == "duration_limit" and result["model_errors"]
    summary = desktop.summarize([result])
    assert summary["task_success_rate"] == 0 and summary["infrastructure_errors"] == 0


async def test_an_unsafe_final_artifact_is_a_task_failure_not_a_broken_harness(
    environment, tmp_path
):
    client, container = environment
    model = MockLLMProvider()
    original = model.understand_instruction

    async def understand(*args, **kwargs):
        container.get_archive.side_effect = lambda _: (iter([archive_file(b"", link=True)]), {})
        return await original(*args, **kwargs)

    model.understand_instruction = understand
    result = {"attempt_id": "unsafe-artifact", "status": "running"}
    await desktop.run_attempt(client, "image", CASES[0], model, ExecutionLimits(), tmp_path, result)
    assert result["status"] == "evaluated" and not result["success"]
    assert "通常ファイル" in result["evidence"]
    assert desktop.summarize([result])["infrastructure_errors"] == 0


async def test_diagnostic_failure_preserves_the_original_error_and_still_cleans_up(
    environment, tmp_path, monkeypatch
):
    client, container = environment

    class BrokenDiagnostic(FakeDisplayBackend):
        def capture_raw(self):
            raise RuntimeError("diagnostic failed")

    monkeypatch.setattr(desktop, "VNCClient", BrokenDiagnostic)
    monkeypatch.setattr(desktop, "prepare_case", Mock(side_effect=RuntimeError("setup failed")))
    result = {"attempt_id": "diagnostic-failure", "status": "running"}
    await desktop.run_attempt(
        client, "image", CASES[0], MockLLMProvider(), ExecutionLimits(), tmp_path, result
    )
    assert result["status"] == "setup_error"
    assert (
        "setup failed" in result["errors"][0] and "diagnostic failed" in result["diagnostic_error"]
    )
    container.remove.assert_called_once_with(force=True)


async def test_cancellation_during_attempt_cleanup_is_saved_and_propagated(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    entered, release = asyncio.Event(), asyncio.Event()
    model = MockLLMProvider()

    async def close():
        entered.set()
        await release.wait()

    model.aclose = close
    monkeypatch.setattr(desktop, "prepare_case", Mock(side_effect=RuntimeError("setup failed")))
    result = {"attempt_id": "stop-cleanup", "status": "running"}
    task = asyncio.create_task(
        desktop.run_attempt(client, "image", CASES[0], model, ExecutionLimits(), tmp_path, result)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert json.loads((tmp_path / "result.json").read_text())["status"] == "interrupted"
        container.remove.assert_called_once_with(force=True)
        client.networks.create.return_value.remove.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_suite_cleanup_failure_prevents_a_successful_exit(environment, tmp_path, monkeypatch):
    client, container = environment
    client.images.get.return_value = SimpleNamespace(id="image-sha")
    client.close.side_effect = RuntimeError("client cleanup failed")
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    code = await desktop.run_suite(arguments(output, mode="scripted", provider=None))
    report = json.loads((output / "report.json").read_text())
    assert code == 2 and report["status"] == "incomplete"
    assert report["summary"]["infrastructure_errors"] == 1
    assert "client cleanup failed" in report["cleanup_errors"][0]


async def test_output_directory_failure_is_reported_and_releases_unused_resources(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    client.images.get.return_value = SimpleNamespace(id="image-sha")
    model = MockLLMProvider()
    model.aclose = AsyncMock()
    monkeypatch.setattr(desktop, "create_llm_provider", Mock(return_value=model))
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path.parent == output and len(path.name) == 16:
            raise PermissionError("attempt output denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert await desktop.run_suite(arguments(output)) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["results"][0]["status"] == "setup_error"
    assert report["summary"]["attempts"] == report["summary"]["infrastructure_errors"] == 1
    assert "attempt output denied" in report["error"]
    model.aclose.assert_awaited_once()
    client.close.assert_called_once()


async def test_provider_creation_interruption_preserves_the_previous_attempt(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    client.images.get.return_value = SimpleNamespace(id="image-sha")
    model = MockLLMProvider()
    model.aclose = AsyncMock()
    factory = Mock(side_effect=[model, asyncio.CancelledError()])
    monkeypatch.setattr(desktop, "create_llm_provider", factory)
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    with pytest.raises(asyncio.CancelledError):
        await desktop.run_suite(arguments(output, repeats=2))
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "interrupted" and report["summary"]["attempts"] == 2
    first, second = report["results"]
    assert first["status"] == "evaluated" and "usage" in first
    assert second["status"] == "interrupted"
    model.aclose.assert_awaited_once()
    client.close.assert_called_once()
    container.remove.assert_called_once_with(force=True)


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancellation_during_suite_cleanup_still_closes_docker_and_saves_the_stop(
    environment, tmp_path, monkeypatch, cancellations
):
    client, container = environment
    client.images.get.side_effect = RuntimeError("image unavailable")
    entered, release = asyncio.Event(), asyncio.Event()
    model = MockLLMProvider()

    async def close():
        entered.set()
        await release.wait()

    model.aclose = AsyncMock(side_effect=close)
    monkeypatch.setattr(desktop, "create_llm_provider", Mock(return_value=model))
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    task = asyncio.create_task(desktop.run_suite(arguments(output)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        report = json.loads((output / "report.json").read_text())
        assert report["status"] == "interrupted" and "image unavailable" in report["error"]
        client.close.assert_called_once()
        model.aclose.assert_awaited_once()
        client.containers.run.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_unreadable_source_metadata_closes_the_provider_without_connecting_docker(
    tmp_path, monkeypatch
):
    model = MockLLMProvider()
    model.aclose = AsyncMock()
    connect = Mock()
    monkeypatch.setattr(desktop, "create_llm_provider", Mock(return_value=model))
    monkeypatch.setattr(desktop.docker, "from_env", connect)
    monkeypatch.setattr(desktop, "source_metadata", Mock(side_effect=OSError("source unreadable")))
    output = tmp_path / "suite"
    assert await desktop.run_suite(arguments(output)) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "incomplete" and not report["results"]
    assert "source unreadable" in report["error"]
    assert report["summary"]["infrastructure_errors"] == 1
    model.aclose.assert_awaited_once()
    connect.assert_not_called()
