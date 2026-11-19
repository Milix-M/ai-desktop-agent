"""Exercise the default Android command path with disposable executables."""

import json
import os
import sys

import pytest

from ai_desktop_agent.server import android_adb as adb


@pytest.fixture
def command(tmp_path, monkeypatch):
    log = tmp_path / "commands.jsonl"
    executable = tmp_path / "adb"
    monkeypatch.setenv("PATH", str(tmp_path))

    def create(body):
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,sys\n"
            f"with open({str(log)!r},'a') as output: "
            "output.write(json.dumps(sys.argv[1:])+'\\n')\n" + body
        )
        executable.chmod(0o700)
        return log

    return create


@pytest.mark.parametrize("operation", ["run", "query", "connect"])
def test_missing_adb_is_a_failed_operation_without_launching_a_child(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setenv("PATH", str(tmp_path))
    if operation == "run":
        assert adb._run_adb("guest:5555", "shell", "true") is False
    elif operation == "query":
        assert adb._run_adb_out("guest:5555", "shell", "getprop") is None
    else:
        assert adb.connect("guest") is None


@pytest.mark.parametrize("code", [0, 1])
def test_shell_exit_code_controls_success_and_query_output(command, code):
    log = command(f"print('  日本語の結果  ')\nsys.exit({code})\n")
    assert adb._run_adb("guest:5555", "shell", "true") is (code == 0)
    assert adb._run_adb_out("guest:5555", "shell", "getprop") == (
        "日本語の結果" if code == 0 else None
    )
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ["-s", "guest:5555", "shell", "true"],
        ["-s", "guest:5555", "shell", "getprop"],
    ]


@pytest.mark.parametrize("operation", ["run", "query", "connect"])
def test_a_timed_out_command_is_reaped(command, tmp_path, operation):
    pid_file = tmp_path / "child.pid"
    log = command(
        "import os,time\n"
        f"with open({str(pid_file)!r},'w') as output: output.write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    if operation == "run":
        assert adb._run_adb("guest:5555", "shell", "true", timeout=0.5) is False
    elif operation == "query":
        assert adb._run_adb_out("guest:5555", "shell", "getprop", timeout=0.5) is None
    else:
        assert adb._run_connect("guest:5555", timeout=0.5) is False
    assert len(log.read_text().splitlines()) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("operation", ["run", "query", "connect"])
def test_an_unexecutable_adb_is_a_failed_operation(command, tmp_path, operation):
    command("pass\n")
    # An existing executable with a missing interpreter fails at Popen, after discovery.
    (tmp_path / "adb").write_text("#!/missing/adb-interpreter\n")
    if operation == "run":
        assert adb._run_adb("guest:5555", "shell", "true") is False
    elif operation == "query":
        assert adb._run_adb_out("guest:5555", "shell", "getprop") is None
    else:
        assert adb.connect("guest") is None
    assert not (tmp_path / "commands.jsonl").exists()


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("ack", ["connected to", "already connected to"])
def test_connect_requires_a_successful_exit_even_when_the_output_mentions_connection(
    command, code, stream, ack
):
    log = command(f"print({ack + ' guest:5555'!r},file=sys.{stream})\nsys.exit({code})\n")
    assert adb.connect("guest") == ("guest:5555" if code == 0 else None)
    assert json.loads(log.read_text()) == ["connect", "guest:5555"]


def test_zero_exit_without_connection_acknowledgement_is_not_connected(command):
    command("print('failed to connect: Connection refused')\n")
    assert adb.connect("guest") is None


@pytest.mark.parametrize("operation", ["run", "query", "connect"])
def test_invalid_diagnostic_bytes_do_not_discard_a_successful_command(command, operation):
    command("sys.stderr.buffer.write(b'\\xff')\nprint('connected to guest:5555')\n")
    if operation == "run":
        assert adb._run_adb("guest:5555", "shell", "true") is True
    elif operation == "query":
        assert adb._run_adb_out("guest:5555", "shell", "getprop") == "connected to guest:5555"
    else:
        assert adb.connect("guest") == "guest:5555"


def test_dialog_dismissal_reports_a_failed_home_key_command(command):
    log = command("print('error: device offline',file=sys.stderr)\nsys.exit(1)\n")
    assert adb.dismiss_dialogs("guest:5555") is False
    assert json.loads(log.read_text()) == ["-s", "guest:5555", "shell", "input", "keyevent", "3"]


def test_sleep_setup_attempts_remaining_settings_after_a_command_failure(command):
    log = command("sys.exit(1 if 'screen_off_timeout' in sys.argv else 0)\n")
    assert adb.setup_sleep("guest:5555") is False
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(commands) == 6
    assert commands[0][-2:] == ["screen_off_timeout", "2147483647"]
    assert commands[-1][-2:] == ["screensaver_activate_on_dock", "0"]
    assert all(args[:3] == ["-s", "guest:5555", "shell"] for args in commands)


def test_default_apk_install_preserves_the_path_as_one_argument(command):
    log = command("print('Success')\n")
    assert adb.install_apk("guest:5555", "/packages/日本語 app.apk") is True
    assert json.loads(log.read_text()) == [
        "-s",
        "guest:5555",
        "install",
        "-r",
        "/packages/日本語 app.apk",
    ]


def test_home_setup_uses_legacy_query_and_setter_when_new_commands_fail(command):
    component = "com.example.desktop/.Home"
    log = command(
        "if 'query-activities' in sys.argv: sys.exit(1)\n"
        f"if 'resolve-activity' in sys.argv: print({component!r})\n"
        "if 'set-home-activity' in sys.argv and 'cmd' in sys.argv: sys.exit(1)\n"
    )
    assert adb.setup_home("guest:5555") is True
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 4
    assert calls[0][3:6] == ["pm", "query-activities", "--components"]
    assert calls[1][3:7] == ["cmd", "package", "resolve-activity", "--brief"]
    assert calls[2][3:] == ["cmd", "package", "set-home-activity", component]
    assert calls[3][3:] == ["pm", "set-home-activity", component]


@pytest.mark.parametrize("interface", ["eth0", "wlan0", "wifi_eth"])
def test_network_probe_skips_loopback_and_checks_other_interfaces(interface):
    visited = []

    def query(_serial, *args):
        current = args[-1]
        visited.append(current)
        return "inet 127.0.0.1/8\n" + (
            "inet 10.0.2.15/24 scope global\n" if current == interface else ""
        )

    assert adb.has_network("guest:5555", query=query) is True
    assert visited[-1] == interface


@pytest.mark.parametrize("output", ["", "inet6 fe80::1/64 scope link\n"])
def test_no_ipv4_address_does_not_satisfy_the_network_setup_check(output):
    assert adb.has_network("guest:5555", query=lambda *_: output) is False


@pytest.mark.parametrize("operation", ["timezone", "wifi", "home", "install"])
def test_callback_failure_does_not_report_successful_setup(operation):
    def failed(*_args):
        raise RuntimeError("ADB transport unavailable")

    if operation == "timezone":
        assert adb.setup_timezone("guest:5555", runner=failed) is False
    elif operation == "wifi":
        assert adb.setup_wifi("guest:5555", runner=failed) is False
    elif operation == "home":
        assert (
            adb.setup_home("guest:5555", runner=failed, query=lambda *_: "com.home/.Home") is False
        )
    else:
        assert adb.install_apk("guest:5555", "/packages/app.apk", query=failed) is False


def test_unexpected_command_failure_does_not_report_a_connected_device(command, monkeypatch):
    command("pass\n")

    def failed(*_args, **_kwargs):
        raise RuntimeError("command transport unavailable")

    monkeypatch.setattr(adb.subprocess, "run", failed)
    assert adb.connect("guest") is None
