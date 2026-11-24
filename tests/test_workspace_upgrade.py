"""Upgrade commands preserve homes and restore each original container on failure."""

import io
import runpy
import signal
import sys
from unittest.mock import Mock

import docker
import pytest
from docker.errors import APIError

from ai_desktop_agent.server import workspace_migration as cli
from ai_desktop_agent.server.networks import LABEL_ISOLATED, LABEL_PROJECT, LABEL_VM_ID
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.workspaces import HOME
from tests.test_vm_pool import _FakeContainer
from tests.test_workspace_backup import environment as environment


@pytest.mark.parametrize(
    "provider,key,seconds,setting",
    [
        ("openai", "", "120", "OPENAI_API_KEY"),
        ("openai", "sk-your-key-here", "120", "OPENAI_API_KEY"),
        ("mock", "", "invalid", "TASK_MAX_SECONDS"),
    ],
)
def test_bad_update_settings_leave_running_services_alone(
    monkeypatch, capsys, provider, key, seconds, setting
):
    workspace = Mock()
    monkeypatch.setattr(cli, "WorkspaceUpgrade", workspace)
    monkeypatch.setattr(sys, "argv", ["workspace", "prepare", "--profile", "container"])
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("TASK_MAX_SECONDS", seconds)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2 and setting in capsys.readouterr().err
    workspace.assert_not_called()


def mount_home(upgrade, source):
    volume = upgrade.volumes.ensure("desktop")
    source.attrs["Mounts"] = [{"Type": "volume", "Name": volume.name, "Destination": HOME}]


@pytest.mark.parametrize("outcome", ["commit", "eof", "interrupt"])
def test_prepare_cli_waits_for_commit_and_restores_a_paused_desktop_on_failure(
    environment, monkeypatch, capsys, outcome
):
    upgrade, source, _ = environment
    mount_home(upgrade, source)
    source.status = "paused"
    monkeypatch.setattr(cli, "WorkspaceUpgrade", lambda: upgrade)
    monkeypatch.setattr(sys, "argv", ["workspace", "prepare", "--profile", "container"])
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    install = Mock()
    monkeypatch.setattr(signal, "signal", install)

    class Reply(io.StringIO):
        def readline(self, *args):
            assert source.status == "exited"
            assert "WORKSPACE_READY" in capsys.readouterr().out
            if outcome == "interrupt":
                assert install.call_args.args[0] == signal.SIGTERM
                install.call_args.args[1](signal.SIGTERM, None)
            return super().readline(*args)

    monkeypatch.setattr(sys, "stdin", Reply("commit\n" if outcome == "commit" else ""))
    if outcome == "commit":
        cli.main()
        assert source.status == "exited"
    else:
        with pytest.raises(RuntimeError if outcome == "eof" else KeyboardInterrupt):
            cli.main()
        assert source.status == "paused"
    assert not source.removed
    metadata = upgrade.pool._load_state("desktop")
    assert upgrade.backups.verified(metadata["last_home_backup"])[1]["source_id"] == source.id


def test_kvm_preparation_stops_the_guest_without_treating_its_disk_as_a_home_archive(environment):
    upgrade, source, _ = environment
    source.attrs["Config"]["Labels"]["com.docker.compose.service"] = "vm"
    with upgrade.transaction():
        upgrade.prepare("kvm")
    assert source.status == "exited" and not source.removed
    source.get_archive.assert_not_called()


@pytest.mark.parametrize("status", ["running", "exited"])
def test_preparation_preserves_its_own_cli_and_other_oneoff_commands(environment, status):
    upgrade, source, client = environment
    mount_home(upgrade, source)
    source.status = status
    client.backend.attrs["Config"]["Labels"].update(
        {"com.docker.compose.service": "backend", "com.docker.compose.oneoff": "true"}
    )
    other = _FakeContainer(
        "other-cli",
        labels={"com.docker.compose.service": "backend", "com.docker.compose.oneoff": "TRUE"},
    )
    client.containers.items.extend([client.backend, other])
    with upgrade.transaction():
        upgrade.prepare("container")
    assert source.status == "exited" and source.stopped is (status == "running")
    assert client.backend.status == other.status == "running"
    assert not client.backend.stopped and not other.stopped


def test_backup_module_entrypoint_saves_the_home_and_releases_its_owned_connection(
    environment, tmp_path, monkeypatch, capsys
):
    upgrade, source, client = environment
    client.close = Mock()
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["workspace", "backup", "desktop"])
    monkeypatch.setattr(signal, "signal", Mock())
    runpy.run_path(cli.__file__, run_name="__main__")
    name = capsys.readouterr().out.strip()
    assert upgrade.backups.verified(name)[1]["source_id"] == source.id
    assert source.status == "running"
    client.close.assert_called_once()


def test_restore_cli_passes_the_selected_archive_inside_its_transaction(environment, monkeypatch):
    upgrade, _, _ = environment
    restore = Mock()
    monkeypatch.setattr(upgrade, "restore", restore)
    monkeypatch.setattr(cli, "WorkspaceUpgrade", lambda: upgrade)
    monkeypatch.setattr(sys, "argv", ["workspace", "restore", "desktop-archive-id"])
    monkeypatch.setattr(signal, "signal", Mock())
    cli.main()
    restore.assert_called_once_with("desktop-archive-id")


def test_foreign_backend_is_rejected_before_stopping_or_saving_a_desktop(environment):
    upgrade, source, client = environment
    client.backend.attrs["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
    with pytest.raises(DockerUnavailableError, match="所属プロジェクト"):
        cli.WorkspaceUpgrade(upgrade.pool, upgrade.root)
    source.pause.assert_not_called()
    assert source.status == "running" and not source.stopped


def test_rejected_stop_is_rolled_back_without_starting_an_already_running_container(environment):
    upgrade, source, _ = environment
    fault = APIError("stop rejected")
    source.stop = Mock(side_effect=fault)
    with pytest.raises(APIError) as raised, upgrade.transaction():
        upgrade.prepare("container")
    assert raised.value is fault and source.status == "running"
    assert not source.started
    source.get_archive.assert_not_called()


@pytest.mark.parametrize("failure", ["restart", "removed"])
def test_rollback_keeps_resuming_other_members_after_a_missing_or_failed_desktop(
    environment, failure, caplog
):
    upgrade, source, client = environment
    mount_home(upgrade, source)
    backend = _FakeContainer("backend-main", labels={"com.docker.compose.service": "backend"})
    client.containers.items.append(backend)
    fault = StorageError("home stream failed")

    def capture(*_args, **_kwargs):
        if failure == "removed":
            source.remove(force=True)
            client.containers.items.remove(source)
        raise fault

    source.get_archive.side_effect = capture
    if failure == "restart":
        source.start = Mock(side_effect=APIError("cannot restart desktop"))
    with pytest.raises(StorageError) as raised, upgrade.transaction():
        upgrade.prepare("container")
    assert raised.value is fault
    assert backend.status == "running" and backend.started
    if failure == "restart":
        assert source.id in caplog.text and source.status == "exited"
    else:
        assert source.removed


@pytest.mark.parametrize("condition", ["foreign", "current", "live", "disconnect-failed"])
def test_network_preconditions_and_failed_detach_preserve_existing_endpoints(
    environment, monkeypatch, condition
):
    upgrade, source, client = environment
    labels = {LABEL_PROJECT: "ai-desktop-agent", LABEL_VM_ID: "desktop", LABEL_ISOLATED: "true"}
    if condition == "foreign":
        labels[LABEL_PROJECT] = "other-project"
    if condition == "current":
        labels["com.docker.compose.network"] = "env_desktop"
    network = client.networks.create("ai-desktop-agent_env_desktop", labels=labels)
    network.connect(source, aliases=["desktop"])
    source.status = "running" if condition == "live" else "exited"
    if condition == "disconnect-failed":
        monkeypatch.setattr(network, "disconnect", Mock(side_effect=APIError("cannot detach")))
    if condition == "current":
        upgrade._reconcile_network("desktop")
    else:
        with pytest.raises(
            APIError if condition == "disconnect-failed" else DockerUnavailableError
        ):
            upgrade._reconcile_network("desktop")
    assert client.networks.get(network.name) is network
    assert source.attrs["NetworkSettings"]["Networks"][network.name]["Aliases"] == ["desktop"]
    assert not source.removed
