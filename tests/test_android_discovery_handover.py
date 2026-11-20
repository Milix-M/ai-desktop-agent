"""Claiming a discovered Android must recheck task ownership and physical identity."""

from unittest.mock import Mock

from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.vm_pool import VmInfo, VmInstance
from tests.test_android_service_lifecycle import target_pool


def test_task_starting_during_discovery_defers_setup_until_the_task_is_idle(tmp_path, monkeypatch):
    pool = target_pool()
    targets = pool.list_vms.return_value
    busy = False

    def discover():
        nonlocal busy
        busy = True
        return targets

    pool.list_vms.side_effect = discover
    state = tmp_path / "setup.json"
    setup = AndroidProvisioner(pool=pool, state_file=state, is_running_check=lambda vm_id: busy)
    provision = Mock(return_value=True)
    monkeypatch.setattr(setup, "provision_vm", provision)
    setup.ensure()
    pool.prepare_vm.assert_not_called()
    provision.assert_not_called()
    assert not state.exists() and setup.needs("and-target")

    busy = False
    pool.list_vms.side_effect = None
    setup.ensure()
    provision.assert_called_once()
    assert provision.call_args.args == ("and-target",)
    assert not setup.needs("and-target")


def test_replacement_after_target_binding_never_receives_the_old_targets_configuration(
    tmp_path, monkeypatch
):
    pool = target_pool()
    original = pool.get_instance.return_value
    replacement = VmInstance(
        info=VmInfo(
            "and-target", "replacement", "running", kind="android", vnc_host="replacement-host"
        ),
        container_id="replacement-container",
    )
    state = tmp_path / "setup.json"
    setup = AndroidProvisioner(pool=pool, state_file=state)
    provision = Mock(return_value=True)
    monkeypatch.setattr(setup, "provision_vm", provision)
    # The original is stable across preparation; it is replaced before the
    # independent identity check immediately preceding the first ADB operation.
    pool.get_instance.side_effect = [original, original, replacement]
    setup.ensure()
    provision.assert_not_called()
    assert not state.exists()

    pool.get_instance.side_effect = None
    pool.get_instance.return_value = replacement
    pool.instance_id.return_value = replacement.container_id
    setup.ensure()
    provision.assert_called_once()
    assert provision.call_args.args == ("replacement-host",)
    assert not setup.needs("and-target")
