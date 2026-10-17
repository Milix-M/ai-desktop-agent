"""ADB経路のテスト（adbバイナリ・subprocessはフェイク）。"""

from ai_desktop_agent.server import android_adb


def _ok(serial, *args):
    return True


def _ng(serial, *args):
    return False


def _boom(serial, *args):
    raise RuntimeError("no adb")


class TestConnect:
    def test_returns_serial(self):
        assert android_adb.connect("android", runner=_ok) == "android:5555"

    def test_failure_returns_none(self):
        assert android_adb.connect("android", runner=_ng) is None

    def test_exception_returns_none(self):
        assert android_adb.connect("android", runner=_boom) is None

    def test_run_connect_parses_output(self, monkeypatch):
        """adb connectはrc=0でも出力判定する（Connection refusedは失敗）。"""
        import subprocess

        class _P:
            def __init__(self, out, err=""):
                self.stdout = out
                self.stderr = err
                self.returncode = 0

        monkeypatch.setattr(android_adb, "_adb_bin", lambda: "/usr/bin/adb")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _P("connected to android:5555\n")
        )
        assert android_adb._run_connect("android:5555") is True
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _P("already connected to android:5555\n")
        )
        assert android_adb._run_connect("android:5555") is True
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _P("Connection refused\n")
        )
        assert android_adb._run_connect("android:5555") is False
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _P("failed to connect to 'x:5555'\n")
        )
        assert android_adb._run_connect("android:5555") is False

    def test_run_connect_uses_no_dash_s(self, monkeypatch):
        """connectは `adb connect <serial>` で `-s` を付けない。"""
        import subprocess

        seen = {}

        class _P:
            stdout = "connected to android:5555"
            stderr = ""
            returncode = 0

        def fake_run(argv, **k):
            seen["argv"] = argv
            return _P()

        monkeypatch.setattr(android_adb, "_adb_bin", lambda: "/usr/bin/adb")
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert android_adb.connect("android") == "android:5555"
        assert seen["argv"][:2] == ["/usr/bin/adb", "connect"]
        assert "-s" not in seen["argv"]


class TestSetupSleep:
    def test_sends_all_sleep_cmds(self):
        calls = []

        def runner(serial, *args):
            calls.append((serial, args))
            return True

        assert android_adb.setup_sleep("android:5555", runner=runner) is True
        flat = [" ".join(a) for _, a in calls]
        assert any("screen_off_timeout" in c for c in flat)
        assert any("stay_on_while_plugged_in" in c for c in flat)
        assert any("stayon" in c for c in flat)
        assert any("screensaver_enabled" in c for c in flat)

    def test_partial_failure_returns_false(self):
        def runner(serial, *args):
            return "stayon" not in args

        assert android_adb.setup_sleep("android:5555", runner=runner) is False

    def test_exception_returns_false(self):
        assert android_adb.setup_sleep("android:5555", runner=_boom) is False


class TestSetupTimezone:
    def test_sends_timezone(self):
        calls = []

        def runner(serial, *args):
            calls.append(args)
            return True

        assert android_adb.setup_timezone("android:5555", runner=runner) is True
        assert any("persist.sys.timezone" in " ".join(a) for a in calls)


class TestSetupWifi:
    def test_enables_and_connects(self):
        calls = []

        def runner(serial, *args):
            calls.append(args)
            return True

        assert android_adb.setup_wifi("android:5555", runner=runner) is True
        flat = [" ".join(a) for a in calls]
        assert any("svc wifi enable" in c for c in flat)
        assert any("connect-network VirtWifi open" in c for c in flat)

    def test_enable_failure_returns_false(self):
        assert android_adb.setup_wifi("android:5555", runner=_ng) is False


class TestSetupHome:
    QUICKSTEP_OUT = (
        "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
        "com.android.launcher3/com.android.quickstep.QuickstepActivity\n"
    )

    def test_picks_quickstep(self):
        calls = []

        def runner(serial, *args):
            calls.append(args)
            return True

        def query(serial, *args):
            return self.QUICKSTEP_OUT

        assert android_adb.setup_home("android:5555", runner=runner, query=query) is True
        flat = [" ".join(a) for a in calls]
        assert any("set-home-activity" in c for c in flat)
        assert any("quickstep" in c.lower() for c in flat)

    def test_no_candidate_returns_false(self):
        assert (
            android_adb.setup_home("android:5555", runner=_ok, query=lambda *a: "nothing")
            is False
        )

    def test_query_failure_returns_false(self):
        def query(serial, *args):
            raise RuntimeError("down")

        assert android_adb.setup_home("android:5555", runner=_ok, query=query) is False

    def test_picks_android_x86_launcher(self):
        """実機9.0-r2の query-activities --components 出力からLauncherを選ぶ。"""
        out = (
            "com.android.launcher3/.Launcher\n"
            "com.farmerbb.taskbar.androidx86/com.farmerbb.taskbar.activity.HomeActivity\n"
            "com.android.settings/.FallbackHome\n"
        )
        assert (
            android_adb._pick_home_component(out)
            == "com.android.launcher3/.Launcher"
        )

        def query(serial, *args):
            return out

        calls = []

        def runner(serial, *args):
            calls.append(args)
            return True

        assert android_adb.setup_home("android:5555", runner=runner, query=query) is True
        flat = [" ".join(a) for a in calls]
        assert any("com.android.launcher3/.Launcher" in c for c in flat)

    def test_queries_pm_query_activities(self):
        """候補列挙に pm query-activities --components を使う。"""
        seen = []

        def query(serial, *args):
            seen.append(args)
            return "com.android.launcher3/.Launcher\n"

        android_adb.setup_home("android:5555", runner=_ok, query=query)
        flat = [" ".join(a) for a in seen]
        assert any("pm" in c and "query-activities" in c for c in flat)

    def test_falls_back_to_pm_on_old_android(self):
        """9.0系で `cmd package set-home-activity` が無い時は `pm` を使う。"""

        def query(serial, *args):
            return self.QUICKSTEP_OUT

        calls = []

        def runner(serial, *args):
            calls.append(args)
            return "cmd" not in args  # cmd失敗・pm成功

        assert android_adb.setup_home("android:5555", runner=runner, query=query) is True
        flat = [" ".join(a) for a in calls]
        assert any("cmd package set-home-activity" in c for c in flat)
        assert any("pm set-home-activity" in c for c in flat)

    def test_both_fail_returns_false(self):
        def query(serial, *args):
            return self.QUICKSTEP_OUT

        assert (
            android_adb.setup_home("android:5555", runner=_ng, query=query) is False
        )

    def test_pick_prefers_launcher_over_first(self):
        out = "com.example.foo/com.example.foo.Bar\ncom.android.launcher3/com.android.Foo\n"
        assert android_adb._pick_home_component(out) == "com.android.launcher3/com.android.Foo"

    def test_pick_none(self):
        assert android_adb._pick_home_component("") is None


class TestDismissDialogs:
    def test_sends_home_key(self):
        calls = []
        def runner(serial, *args):
            calls.append(args)
            return True
        assert android_adb.dismiss_dialogs("s:5555", runner=runner) is True
        assert any("keyevent" in " ".join(a) for a in calls)

    def test_failure_returns_false(self):
        assert android_adb.dismiss_dialogs("s:5555", runner=_boom) is False


class TestHasNetwork:
    def test_eth0_with_ipv4(self):
        def query(serial, *args):
            if "eth0" in args:
                return "4: eth0: <UP> mtu 1500\n    inet 10.0.2.15/24 scope global eth0"
            return "no inet"
        assert android_adb.has_network("s:5555", query=query) is True

    def test_no_ipv4(self):
        assert android_adb.has_network("s:5555", query=lambda *a: "no inet here") is False

    def test_query_failure(self):
        def q(*a):
            raise RuntimeError("down")
        assert android_adb.has_network("s:5555", query=q) is False


class TestInstallApk:
    def test_success(self):
        assert (
            android_adb.install_apk("android:5555", "/a/F-Droid.apk", query=lambda *a: "Success")
            is True
        )

    def test_already_exists_counts_as_success(self):
        out = "Failure [INSTALL_FAILED_ALREADY_EXISTS]"
        assert android_adb.install_apk("android:5555", "/a/F.apk", query=lambda *a: out) is True

    def test_failure(self):
        assert (
            android_adb.install_apk("android:5555", "/a/F.apk", query=lambda *a: "Failure [OTHER]")
            is False
        )

    def test_no_adb_returns_false(self):
        assert android_adb.install_apk("android:5555", "/a/F.apk", query=lambda *a: None) is False
