"""Android初期設定の回帰テスト。

- ADBのみで行い、VNCタップ列は使わないこと。
- seed同梱なしで空dataから設定できること。
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestNoVncTaps:
    def test_provision_has_no_vnc_taps(self):
        """プロビジョニングにVNCタップ列を含めない（禁止の回帰防止）。"""
        src = (
            PROJECT_ROOT / "src" / "ai_desktop_agent" / "server" / "android_provision.py"
        ).read_text()
        for banned in ("VNCClient", "mouse_click", "key_press", "capture_screen", "_tap_word"):
            assert banned not in src, f"VNCタップ列の残骸: {banned}"

    def test_provision_uses_adb(self, tmp_path, monkeypatch):
        from ai_desktop_agent.server.android_provision import AndroidProvisioner

        p = AndroidProvisioner(
            pool=None,
            state_file=tmp_path / "s.json",
            nap=lambda s: None,
        )
        monkeypatch.setattr(p, "_wait_adb_serial", lambda host: "android:5555")
        monkeypatch.setattr(p, "_apk_paths", lambda: [])

        import ai_desktop_agent.server.android_adb as adb_mod

        calls = []
        monkeypatch.setattr(adb_mod, "setup_sleep", lambda s: calls.append("sleep") or True)
        monkeypatch.setattr(adb_mod, "setup_timezone", lambda s: calls.append("tz") or True)
        monkeypatch.setattr(adb_mod, "setup_wifi", lambda s: calls.append("wifi") or True)
        monkeypatch.setattr(adb_mod, "setup_home", lambda s: calls.append("home") or True)
        assert p.provision_vm("android") is True
        assert calls == ["sleep", "tz", "wifi", "home"]

    def test_no_seed_required(self):
        """entrypointは空data.imgを自動作成し、設定はADBで投入する。"""
        content = (PROJECT_ROOT / "android" / "entrypoint.sh").read_text()
        assert "mkfs.ext4" in content, "空data.imgの自動作成が必要です"
        # seedファイルの同梱・コピーはしない（ADBで設定する）。
        # 説明コメントの「同梱なし」言及は許可し、実体操作のみ禁止する。
        low = content.lower()
        for banned in ("seed.img", "seed.tar", "seed.zip", "cp .*seed", "unzip.*seed"):
            assert banned not in low, f"seed同梱の残骸: {banned}"
        assert "mkfs.ext4" in content

    def test_backend_has_adb_client(self):
        content = (PROJECT_ROOT / "Dockerfile").read_text()
        assert "android-tools-adb" in content, "ADB経路用のadbクライアントが必要です"
