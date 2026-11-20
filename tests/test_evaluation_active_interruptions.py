"""An interrupted active attempt must retain evidence and release its resources."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.evaluation import desktop
from ai_desktop_agent.evaluation.cases import CASES
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_desktop_evaluation import environment as environment
from tests.test_evaluation_failures import arguments


async def test_parent_cancellation_during_model_work_is_saved_and_releases_every_resource(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    entered, interrupted = asyncio.Event(), asyncio.Event()

    class WaitingModel(MockLLMProvider):
        async def understand_instruction(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                interrupted.set()

    model = WaitingModel()
    model.aclose = AsyncMock()
    display = FakeDisplayBackend()
    monkeypatch.setattr(desktop, "VNCClient", lambda: display)
    client.images.get.return_value = SimpleNamespace(id="image-sha")
    monkeypatch.setattr(desktop, "create_llm_provider", Mock(return_value=model))
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    task = asyncio.create_task(desktop.run_suite(arguments(output)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    report = json.loads((output / "report.json").read_text())
    attempt = report["results"][0]
    saved = json.loads((output / attempt["attempt_id"] / "result.json").read_text())
    assert report["status"] == attempt["status"] == saved["status"] == "interrupted"
    assert interrupted.is_set() and attempt["stop_reason"] == "cancelled"
    assert report["summary"]["attempts"] == 1
    assert report["summary"]["evaluated_attempts"] == report["summary"]["successes"] == 0
    assert "agent_success" not in attempt and not display.is_connected
    assert not (output / attempt["attempt_id"] / "final.png").exists()
    model.aclose.assert_awaited_once()
    container.remove.assert_called_once_with(force=True)
    client.networks.create.return_value.remove.assert_called_once_with()
    client.close.assert_called_once_with()


async def test_failed_initial_connection_never_attempts_a_disconnected_diagnostic_capture(
    environment, tmp_path, monkeypatch
):
    client, container = environment

    class UnavailableDisplay(FakeDisplayBackend):
        def connect(self, *args, **kwargs):
            raise ConnectionError("desktop refused the initial connection")

    display = UnavailableDisplay()
    display.disconnect()
    capture = Mock(wraps=display.capture_raw)
    monkeypatch.setattr(display, "capture_raw", capture)
    monkeypatch.setattr(desktop, "VNCClient", lambda: display)
    model = MockLLMProvider()
    model.understand_instruction = AsyncMock(wraps=model.understand_instruction)
    model.aclose = AsyncMock()
    result = {"attempt_id": "connection-refused", "status": "running"}
    await desktop.run_attempt(client, "image", CASES[0], model, ExecutionLimits(), tmp_path, result)
    saved = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == saved["status"] == "setup_error"
    assert "initial connection" in result["errors"][0]
    assert "diagnostic_error" not in result and "agent_success" not in result
    assert not (tmp_path / "error.png").exists()
    capture.assert_not_called()
    model.understand_instruction.assert_not_awaited()
    model.aclose.assert_awaited_once()
    container.remove.assert_called_once_with(force=True)
    client.networks.create.return_value.remove.assert_called_once_with()


async def test_later_provider_creation_failure_preserves_the_completed_attempt(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    client.images.get.return_value = SimpleNamespace(id="image-sha")
    model = MockLLMProvider()
    model.aclose = AsyncMock()
    factory = Mock(side_effect=[model, ValueError("second provider configuration is invalid")])
    monkeypatch.setattr(desktop, "create_llm_provider", factory)
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "suite"
    assert await desktop.run_suite(arguments(output, repeats=2)) == 2
    report = json.loads((output / "report.json").read_text())
    first, second = report["results"]
    assert report["status"] == "incomplete" and "second provider" in report["error"]
    assert first["status"] == "evaluated" and "usage" in first
    assert second["status"] == "setup_error" and "agent_success" not in second
    assert report["summary"]["attempts"] == 2
    assert report["summary"]["evaluated_attempts"] == 1
    assert report["summary"]["infrastructure_errors"] == 1
    model.aclose.assert_awaited_once()
    client.close.assert_called_once_with()
    container.remove.assert_called_once_with(force=True)
