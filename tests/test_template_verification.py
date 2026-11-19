"""Sampled pixels must never stand in for verification of the whole template."""

import io

from PIL import Image

from ai_desktop_agent.vm.matcher import find_template


def png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_matching_stride_samples_cannot_hide_different_pixels():
    template = Image.new("L", (8, 8), 255)
    screen = Image.new("L", (8, 8), 0)
    for y in (0, 4):
        for x in (0, 4):
            screen.putpixel((x, y), 255)
    assert find_template(png(screen), png(template), threshold=0.9) is None


def test_exact_full_image_match_is_preserved():
    image = Image.new("L", (8, 8), 255)
    assert find_template(png(image), png(image)) == (4, 4, 1.0)
