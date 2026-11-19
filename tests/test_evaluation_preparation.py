"""Validate the prepared guest data, startup deadline and bounded evidence capture."""

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_desktop_agent.evaluation import desktop
from ai_desktop_agent.evaluation.cases import CASES, check_case
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def guest(case):
    container = Mock()
    container.put_archive.return_value = True

    def execute(command, **kwargs):
        output = b""
        if command == ["id", "-u"]:
            output = b"1001\n"
        elif command == ["id", "-g"]:
            output = b"1002\n"
        elif command == ["wmctrl", "-l"]:
            title = "evaluation-case" if case.renamed else case.filename
            output = ("0xabc 0 host " + title).encode()
        return SimpleNamespace(exit_code=0, output=output)

    container.exec_run.side_effect = execute
    return container


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_guest_preparation_returns_the_exact_seed_files_and_preserves_ownership(case):
    container = guest(case)
    directory = "/home/agent/Documents/evaluation-case"
    files = desktop.prepare_case(container, case, directory)
    destination, data = container.put_archive.call_args.args
    assert destination == directory
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert {member.name for member in archive} == set(files)
        for member in archive:
            assert member.isfile() and member.uid == 1001 and member.gid == 1002
            assert member.mode == 0o600
            assert archive.extractfile(member).read() == files[member.name]
    baseline = {name: files.get(name) for name in desktop.filenames(case)}
    assert not check_case(case, baseline)[0]
    commands = [call.args[0] for call in container.exec_run.call_args_list]
    mousepad = ["mousepad", "--disable-server", directory + "/" + case.filename]
    expected = {
        "text-edit": mousepad,
        "text-append": mousepad,
        "file-rename": ["thunar", directory],
        "spreadsheet-total": [
            "libreoffice",
            "--nologo",
            "--norestore",
            "--calc",
            directory + "/" + case.filename,
        ],
    }[case.id]
    assert expected in commands
    assert commands[-1] == ["xdotool", "windowactivate", "--sync", "0xabc"]
    for call in container.exec_run.call_args_list:
        assert call.kwargs["user"] == "agent" and call.kwargs["environment"] == desktop.ENV


def test_a_rejected_archive_stops_before_opening_the_guest_app():
    container = guest(CASES[0])
    container.put_archive.return_value = False
    with pytest.raises(RuntimeError, match="初期ファイル"):
        desktop.prepare_case(container, CASES[0], "/home/agent/Documents/evaluation-case")
    commands = [call.args[0][0] for call in container.exec_run.call_args_list]
    assert "mousepad" not in commands and "wmctrl" not in commands


def test_guest_command_failure_keeps_the_diagnostic_and_does_not_reject_detached_launches():
    container = Mock()
    result = SimpleNamespace(exit_code=1, output=b"command failed\xff")
    container.exec_run.return_value = result
    with pytest.raises(RuntimeError, match="command failed"):
        desktop.exec_guest(container, ["bad-command"])
    assert desktop.exec_guest(container, ["bad-command"], detach=True) is result


def test_an_unrelated_window_does_not_satisfy_the_preparation_deadline(monkeypatch):
    container = guest(CASES[0])
    original = container.exec_run.side_effect
    container.exec_run.side_effect = lambda command, **kwargs: (
        SimpleNamespace(exit_code=0, output=b"0x123 0 host unrelated window")
        if command == ["wmctrl", "-l"]
        else original(command, **kwargs)
    )
    clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 31]), sleep=Mock())
    monkeypatch.setattr(desktop, "time", clock)
    with pytest.raises(RuntimeError, match="ウィンドウ"):
        desktop.prepare_case(container, CASES[0], "/home/agent/Documents/evaluation-case")
    clock.sleep.assert_called_once_with(0.1)
    assert not any(call.args[0][0] == "xdotool" for call in container.exec_run.call_args_list)


@pytest.mark.parametrize("status", ["exited", "dead", "removing"])
def test_terminal_guests_cannot_pass_with_a_stale_healthy_check(status):
    container = Mock(status=status, attrs={"State": {"Health": {"Status": "healthy"}}})
    with pytest.raises(RuntimeError, match="起動しません"):
        desktop.wait_desktop(container)
    container.reload.assert_called_once()


@pytest.mark.parametrize("status", ["paused", "restarting", "created"])
def test_nonrunning_guests_do_not_become_ready_from_a_stale_health_result(monkeypatch, status):
    container = Mock(status=status, attrs={"State": {"Health": {"Status": "healthy"}}})
    clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 61]), sleep=Mock())
    monkeypatch.setattr(desktop, "time", clock)
    with pytest.raises(RuntimeError, match="上限"):
        desktop.wait_desktop(container)
    clock.sleep.assert_called_once_with(0.2)


def test_startup_waits_for_both_a_running_guest_and_a_healthy_check(monkeypatch):
    container = Mock(status="restarting", attrs={"State": {"Health": {"Status": "healthy"}}})
    states = iter(["restarting", "running"])
    container.reload.side_effect = lambda: setattr(container, "status", next(states))
    clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1]), sleep=Mock())
    monkeypatch.setattr(desktop, "time", clock)
    desktop.wait_desktop(container)
    assert container.reload.call_count == 2
    clock.sleep.assert_called_once_with(0.2)


def test_recording_limits_saved_frames_but_still_returns_fresh_screenshots(tmp_path):
    backend = FakeDisplayBackend(16, 16)
    recording = desktop.RecordingDisplay(backend, tmp_path)
    for index in range(66):
        frame = recording.capture_screen() if index % 2 == 0 else recording.capture_raw()
        assert frame.frame_number == index + 1
    assert backend.screenshots_taken == 66 and recording.dropped == 2
    assert len(recording.frames) == len(list(tmp_path.iterdir())) == 64
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.iterdir())
    assert recording.frames[0]["overlay"] is True and recording.frames[1]["overlay"] is False
