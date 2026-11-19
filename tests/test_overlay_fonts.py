"""Platform font selection must preserve visible coordinate labels and PNG metadata."""

import sys

import pytest
from PIL import ImageChops, ImageFont

from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.overlay import add_coordinate_overlay


@pytest.mark.parametrize("menlo_available", [False, True], ids=["fallback", "menlo"])
def test_simulated_macos_renders_coordinate_labels_with_the_first_available_font(
    monkeypatch, menlo_available
):
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
    candidates = []

    def load(path, size):
        candidates.append((path, size))
        if path == "/System/Library/Fonts/Menlo.ttc" and not menlo_available:
            raise OSError("Menlo is not installed")
        return font

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ImageFont, "truetype", load)
    source = FakeDisplayBackend(screen_width=120, screen_height=80).capture_raw()
    result = add_coordinate_overlay(source, show_grid=False, font_size=12)
    assert candidates[0] == ("/System/Library/Fonts/Menlo.ttc", 12)
    assert len(candidates) == (1 if menlo_available else 2)
    if not menlo_available:
        assert candidates[1] == ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
    assert (result.width, result.height) == (source.width, source.height)
    assert result.timestamp == source.timestamp and result.frame_number == source.frame_number
    with source.to_pil() as original, result.to_pil() as rendered:
        assert rendered.format == "PNG" and rendered.size == (120, 80)
        # This region contains the first coordinate glyph, away from the frame.
        difference = ImageChops.difference(original, rendered).crop((3, 3, 24, 16))
        assert difference.getbbox() is not None
        assert rendered.getpixel((60, 40)) == original.getpixel((60, 40))
