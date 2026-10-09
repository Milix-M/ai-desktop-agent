"""ADBバックエンドのテスト（adb実物不要。runner差し替え）。"""

import pytest

from ai_desktop_agent.vm.adb_backend import AdbBackend, _escape_input_text


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.screencap_png: bytes = b""
        self.boot_completed = "1"

    def __call__(self, args, *, timeout=30.0, binary=False):
        self.calls.append(list(args))
        cmd = list(args)
        if cmd[-2:] == ["shell", "getprop sys.boot_completed"]:
            return _Result(0, self.boot_completed)
        if "exec-out" in cmd:
            return _Result(0, self.screencap_png)
        if "wait-for-device" in cmd:
            return _Result(0, "")
        if cmd[:2] == ["adb", "connect"]:
            return _Result(0, "already connected")
        if cmd[:2] == ["adb", "disconnect"]:
            return _Result(0, "")
        return _Result(0, "")


def _png_bytes(w=1080, h=1920):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def runner():
    return _FakeRunner()


@pytest.fixture
def backend(runner):
    b = AdbBackend(runner=runner)
    b.connect("android", 5555)
    return b


def test_connect_and_disconnect(runner):
    b = AdbBackend(runner=runner)
    assert b.is_connected is False
    b.connect("android", 5555)
    assert b.is_connected is True
    assert b.serial == "android:5555"
    assert runner.calls[0][:3] == ["adb", "connect", "android:5555"]
    b.disconnect()
    assert b.is_connected is False


def test_connect_failure(runner):
    class _Boom(_FakeRunner):
        def __call__(self, args, *, timeout=30.0, binary=False):
            return _Result(1, "", "refused")

    with pytest.raises(RuntimeError, match="adb connect"):
        AdbBackend(runner=_Boom()).connect("android", 5555)


def test_tap_maps_click(backend, runner):
    backend.mouse_click(100, 200)
    assert runner.calls[-1][-2:] == ["shell", "input tap 100 200"]
    assert (backend._cursor_x, backend._cursor_y) == (100, 200)


def test_click_requires_coords(backend):
    with pytest.raises(ValueError, match="座標必須"):
        backend.mouse_click(None, None)


def test_drag_maps_swipe(backend, runner):
    backend.mouse_drag(0, 0, 10, 500)
    assert runner.calls[-1][-1].endswith("input swipe 0 0 10 500 300")


def test_scroll_direction(backend, runner):
    backend.mouse_scroll("up", 1)
    assert "input swipe" in runner.calls[-1][-1]
    with pytest.raises(ValueError, match="up/down"):
        backend.mouse_scroll("left", 1)


def test_key_mapping(backend, runner):
    backend.key_press("enter")
    assert runner.calls[-1][-1].endswith("input keyevent KEYCODE_ENTER")
    backend.key_press("escape")
    assert runner.calls[-1][-1].endswith("input keyevent KEYCODE_BACK")
    backend.key_press("a")
    assert runner.calls[-1][-1].endswith("input text a")
    with pytest.raises(ValueError, match="未対応のキー"):
        backend.key_press("f13")


def test_key_hold_unsupported(backend):
    with pytest.raises(NotImplementedError):
        backend.key_down("a")
    with pytest.raises(NotImplementedError):
        backend.key_up("a")


def test_type_text_ascii_and_escape(backend, runner):
    backend.type_text("hi there (x)")
    assert runner.calls[-1][-1].endswith("input text hi%sthere%s\\(x\\)")


def test_type_text_rejects_japanese(backend):
    with pytest.raises(ValueError, match="日本語"):
        backend.type_text("こんにちは")


def test_escape_helper():
    assert _escape_input_text("a b") == "a%sb"


def test_capture_screen(backend, runner):
    runner.screencap_png = _png_bytes()
    ss = backend.capture_screen()
    assert (ss.width, ss.height) == (1080, 1920)
    assert ss.image_bytes  # オーバーレイ済みPNG


def test_capture_failure(backend, runner):
    runner.screencap_png = b""
    with pytest.raises(RuntimeError, match="screencap"):
        backend.capture_raw()


def test_capture_region(backend, runner):
    runner.screencap_png = _png_bytes()
    ss = backend.capture_region(0, 0, 100, 100)
    assert ss.image_bytes


def test_wait_boot_ok(backend, runner, monkeypatch):
    monkeypatch.setattr("ai_desktop_agent.vm.adb_backend.time.sleep", lambda s: None)
    backend.wait_boot(timeout=5.0)


def test_wait_boot_timeout(backend, runner, monkeypatch):
    runner.boot_completed = "0"
    monkeypatch.setattr("ai_desktop_agent.vm.adb_backend.time.sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        backend.wait_boot(timeout=0.01)


def test_not_connected_operations():
    b = AdbBackend(runner=_FakeRunner())
    with pytest.raises(RuntimeError, match="接続されていません"):
        b.capture_raw()
    with pytest.raises(RuntimeError, match="接続されていません"):
        b.type_text("hi")


def test_default_runner_signature():
    import inspect

    from ai_desktop_agent.vm.adb_backend import _default_runner

    assert "timeout" in inspect.signature(_default_runner).parameters
