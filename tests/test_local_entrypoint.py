"""The developer CLI must retain the production server's authentication/state bounds."""

import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn

from ai_desktop_agent import main as cli
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol


@pytest.mark.parametrize(
    "arguments,host,port,reload",
    [
        ([], "127.0.0.1", 8081, False),
        (["--host", "::1", "--port", "8181", "--reload"], "::1", 8181, True),
    ],
)
def test_cli_preserves_single_process_state_and_websocket_limits(
    monkeypatch, arguments, host, port, reload
):
    monkeypatch.setattr(sys, "argv", ["ai-desktop-agent", *arguments])
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    cli.main()
    assert run.call_args.args == ("ai_desktop_agent.server.app:app",)
    options = run.call_args.kwargs
    assert options["host"] == host and options["port"] == port and options["reload"] is reload
    assert options["workers"] == 1
    assert options["proxy_headers"] is False
    assert options["ws"] is AgentWebSocketProtocol
    assert options["ws_max_size"] == 1024 * 1024 and options["ws_max_queue"] == 4


def test_module_entrypoint_uses_the_same_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ai-desktop-agent"])
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    runpy.run_path(str(Path(cli.__file__)), run_name="__main__")
    assert run.call_count == 1
    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_invalid_port_is_rejected_without_starting_a_server(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ai-desktop-agent", "--port", "invalid"])
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    run.assert_not_called()
