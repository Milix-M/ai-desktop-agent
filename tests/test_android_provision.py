"""AndroidのADB投入のテスト（VNCタップ列なし）。"""

import pytest

from ai_desktop_agent.server.android_provision import AndroidProvisioner


class _FakePool:
    def __init__(self, vms=None):
        self._vms = vms or []

    def list_vms(self):
        return list(self._vms)

    def get_vm(self, vm_id):
        for vm in self._vms:
            if vm.id == vm_id:
                return vm
        return None


class _FakeVm:
    def __init__(self, id, kind="android", status="running", vnc_host=""):
        self.id = id
        self.kind = kind
        self.status = status
        self.vnc_host = vnc_host or id


def _prov(tmp_path, **kw):
    kw.setdefault("state_file", tmp_path / "s.json")
    kw.setdefault("nap", lambda s: None)
    return AndroidProvisioner(pool=_FakePool(), **kw)


class TestProvisionVm:
    def test_all_steps_via_adb(self, tmp_path, monkeypatch):
        import ai_desktop_agent.server.android_adb as adb_mod

        (tmp_path / "apks").mkdir()
        (tmp_path / "apks" / "F-Droid.apk").write_bytes(b"fake")
        p = AndroidProvisioner(
            pool=_FakePool(),
            state_file=tmp_path / "s.json",
            nap=lambda s: None,
            apks_dir=tmp_path / "apks",
        )
        monkeypatch.setattr(p, "_wait_adb_serial", lambda host: "android:5555")
        calls = []
        monkeypatch.setattr(adb_mod, "setup_sleep", lambda s: calls.append("sleep") or True)
        monkeypatch.setattr(adb_mod, "setup_timezone", lambda s: calls.append("timezone") or True)
        monkeypatch.setattr(adb_mod, "setup_wifi", lambda s: calls.append("wifi") or True)
        monkeypatch.setattr(adb_mod, "setup_home", lambda s: calls.append("home") or True)
        monkeypatch.setattr(adb_mod, "dismiss_dialogs", lambda s: True)
        installed = []
        monkeypatch.setattr(adb_mod, "install_apk", lambda s, a: installed.append(a) or True)
        assert p.provision_vm("android") is True
        assert calls == ["sleep", "timezone", "wifi", "home"]
        assert installed == [str(tmp_path / "apks" / "F-Droid.apk")]

    def test_unreachable_returns_false(self, tmp_path, monkeypatch):
        p = _prov(tmp_path)
        monkeypatch.setattr(p, "_wait_adb_serial", lambda host: None)
        assert p.provision_vm("android") is False

    def test_failed_step_returns_false(self, tmp_path, monkeypatch):
        import ai_desktop_agent.server.android_adb as adb_mod

        p = _prov(tmp_path, apks_dir=tmp_path / "empty")
        (tmp_path / "empty").mkdir()
        monkeypatch.setattr(p, "_wait_adb_serial", lambda host: "android:5555")
        monkeypatch.setattr(adb_mod, "setup_sleep", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_timezone", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_wifi", lambda s: False)
        monkeypatch.setattr(adb_mod, "has_network", lambda s: False)
        monkeypatch.setattr(adb_mod, "setup_home", lambda s: True)
        monkeypatch.setattr(adb_mod, "dismiss_dialogs", lambda s: True)
        assert p.provision_vm("android") is False

    def test_wifi_fallback_to_ethernet(self, tmp_path, monkeypatch):
        """Wi-Fi失敗でもEthernet疎通があれば成功扱い(VIRT_WIFI=0既定)。"""
        import ai_desktop_agent.server.android_adb as adb_mod

        p = _prov(tmp_path, apks_dir=tmp_path / "empty")
        (tmp_path / "empty").mkdir()
        monkeypatch.setattr(p, "_wait_adb_serial", lambda host: "android:5555")
        monkeypatch.setattr(adb_mod, "setup_sleep", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_timezone", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_wifi", lambda s: False)
        monkeypatch.setattr(adb_mod, "has_network", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_home", lambda s: True)
        monkeypatch.setattr(adb_mod, "dismiss_dialogs", lambda s: True)
        assert p.provision_vm("android") is True

    def test_no_apks_skips_install(self, tmp_path, monkeypatch):
        import ai_desktop_agent.server.android_adb as adb_mod

        (tmp_path / "empty").mkdir()
        p = _prov(tmp_path, apks_dir=tmp_path / "empty")
        monkeypatch.setattr(p, "_wait_adb_serial", lambda host: "android:5555")
        monkeypatch.setattr(adb_mod, "setup_sleep", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_timezone", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_wifi", lambda s: True)
        monkeypatch.setattr(adb_mod, "setup_home", lambda s: True)
        monkeypatch.setattr(adb_mod, "dismiss_dialogs", lambda s: True)
        called = []
        monkeypatch.setattr(adb_mod, "install_apk", lambda s, a: called.append(a) or True)
        assert p.provision_vm("android") is True
        assert called == []


class TestState:
    def test_needs_then_done(self, tmp_path, monkeypatch):
        p = AndroidProvisioner(
            pool=_FakePool(),
            state_file=tmp_path / "s.json",
            nap=lambda s: None,
        )
        monkeypatch.setattr(p, "_data_id", lambda vm: "d1")
        assert p.needs("android") is True
        p.mark_done("android")
        assert p.needs("android") is False
        monkeypatch.setattr(p, "_data_id", lambda vm: "d2")
        assert p.needs("android") is True

    def test_ensure_skips_done_and_busy(self, tmp_path, monkeypatch):
        pool = _FakePool([_FakeVm("android"), _FakeVm("and-x1")])
        p = AndroidProvisioner(
            pool=pool,
            state_file=tmp_path / "s.json",
            nap=lambda s: None,
            is_running_check=lambda vm: vm == "and-x1",
        )
        monkeypatch.setattr(p, "_data_id", lambda vm: "same")
        p.mark_done("android")
        calls = []
        monkeypatch.setattr(p, "provision_vm", lambda host: calls.append(host) or True)
        p.ensure()
        assert calls == []


class TeststartStop:
    @pytest.mark.asyncio
    async def test_disabled_by_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANDROID_PROVISION", "0")
        p = AndroidProvisioner(pool=_FakePool(), state_file=tmp_path / "s.json")
        await p.start()
        assert p._task is None
        await p.stop()
