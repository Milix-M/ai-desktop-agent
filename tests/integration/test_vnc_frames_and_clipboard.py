"""Real VNC regions and delayed clipboard replies preserve display boundaries."""

import asyncio
import json
import os
import struct
import threading

import pytest
from PIL import Image
from twisted.internet import reactor

from ai_desktop_agent.vm.clipboard import REQUEST
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime
from tests.integration.test_model_metadata import terminal as terminal

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


async def test_region_capture_returns_the_same_pixels_as_the_real_screen_region(
    desktop, terminal, tmp_path
):
    display, _ = terminal
    full = await asyncio.to_thread(display.capture_raw)
    proof = {"screen_size": [full.width, full.height]}
    try:
        region = await asyncio.to_thread(display.capture_region, 100, 200, 120, 60)
    except Exception as error:
        proof.update(error=type(error).__name__, message=str(error))
        (tmp_path / "region-capture-proof.json").write_text(json.dumps(proof))
        raise
    with full.to_pil() as original, region.to_pil() as cropped:
        expected = original.crop((100, 200, 220, 260))
        proof.update(
            region_size=list(cropped.size),
            format=cropped.format,
            pixels_match=expected.tobytes() == cropped.tobytes(),
        )
    (tmp_path / "region-capture-proof.json").write_text(json.dumps(proof))
    assert proof["pixels_match"] and proof["format"] == "PNG"
    assert proof["region_size"] == [120, 60]
    following = await asyncio.to_thread(display.capture_raw)
    assert (following.width, following.height) == (full.width, full.height)


async def test_a_real_screen_rejected_by_the_decoder_is_not_reported_as_a_valid_capture(
    desktop, terminal, tmp_path, monkeypatch
):
    display, _ = terminal
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10000)
    rejected, dimensions = False, None
    try:
        image = await asyncio.to_thread(display.capture_raw)
        dimensions = [image.width, image.height]
    except RuntimeError as error:
        assert "画像" in str(error)
        rejected = True
    (tmp_path / "screen-decoder-proof.json").write_text(
        json.dumps({"decoder_rejected": rejected, "reported_dimensions": dimensions})
    )
    assert rejected


async def test_clipboard_timeout_closes_the_real_connection_before_a_delayed_paste_can_finish(
    desktop, terminal, tmp_path, monkeypatch
):
    display, proof = terminal
    await asyncio.to_thread(display.type_text, f"cat > {proof}\n")
    async with asyncio.timeout(5):
        while desktop.exec_run(["test", "-e", proof]).exit_code != 0:
            await asyncio.sleep(0.05)
    assert desktop.exec_run(["cat", proof]).output == b""
    protocol = display._client.protocol
    assert await asyncio.to_thread(protocol.clipboard_ready.wait, 2)
    assert protocol.clipboard_supported
    original = protocol._handle_clipboard
    held, requested = [], threading.Event()

    def withhold_request(block):
        flags = struct.unpack("!I", block[:4])[0]
        if flags & 0xFF000000 == REQUEST:
            held.append(block)
            requested.set()
            protocol.expect(protocol._handleConnection, 1)
        else:
            original(block)

    monkeypatch.setattr(protocol, "_handle_clipboard", withhold_request)
    with pytest.raises(RuntimeError, match="貼り付け応答がありません"):
        await asyncio.to_thread(display.type_text, "遅れた貼り付け\n")
    assert requested.is_set() and held
    connected_after_timeout = display.is_connected
    replayed = threading.Event()

    def release_delayed_request():
        try:
            original(held[0])
        finally:
            replayed.set()

    reactor.callFromThread(release_delayed_request)
    assert await asyncio.to_thread(replayed.wait, 2)
    await asyncio.sleep(0.3)
    contents = desktop.exec_run(["cat", proof]).output
    (tmp_path / "late-clipboard-proof.json").write_text(
        json.dumps(
            {
                "connected_after_timeout": connected_after_timeout,
                "withheld_requests": len(held),
                "late_contents": contents.decode("utf-8", errors="replace"),
            },
            ensure_ascii=False,
        )
    )
    assert not connected_after_timeout and contents == b""
