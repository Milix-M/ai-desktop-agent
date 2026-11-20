"""Template scores cannot authorize a location without comparable image variation."""

import io

import pytest
from PIL import Image

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.matcher import find_template
from ai_desktop_agent.vm.screenshot import Screenshot


def png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def images(varying):
    flat = Image.new("L", (8, 8), 128)
    pattern = flat.copy()
    pattern.putpixel((1, 1), 129)
    return (flat, pattern) if varying == "template" else (pattern, flat)


@pytest.mark.parametrize("varying", ["screen", "template"])
@pytest.mark.parametrize("stride", [1, 4])
def test_one_flat_image_cannot_match_a_pattern_even_with_a_small_pixel_difference(varying, stride):
    screen, template = images(varying)
    assert find_template(png(screen), png(template), threshold=0.9, max_stride=stride) is None


@pytest.mark.parametrize("varying", ["screen", "template"])
def test_executor_does_not_offer_a_coordinate_for_an_undefined_correlation(varying):
    screen, template = images(varying)
    display = FakeDisplayBackend(8, 8)
    display.capture_raw = lambda: Screenshot(png(screen), 8, 8)
    try:
        assert ActionExecutor(display).locate(png(template)) is None
        assert not display.clicks and not display.mouse_moves
    finally:
        display.disconnect()


@pytest.mark.parametrize(
    "threshold",
    [float("nan"), float("inf"), -float("inf"), -0.1, 1.1, True, None, "0.9", 10**1000],
    ids=[
        "nan",
        "infinity",
        "negative-infinity",
        "negative",
        "past-one",
        "boolean",
        "none",
        "text",
        "overflow",
    ],
)
def test_invalid_thresholds_are_rejected_before_comparing_images(threshold):
    image = png(Image.new("L", (8, 8), 128))
    with pytest.raises(ValueError, match="threshold"):
        find_template(image, image, threshold=threshold)


@pytest.mark.parametrize("stride", [0, -1, True, None, 1.5, "4"])
def test_search_stride_requires_a_positive_integer(stride):
    image = png(Image.new("L", (8, 8), 128))
    with pytest.raises(ValueError, match="max_stride"):
        find_template(image, image, max_stride=stride)


@pytest.mark.parametrize("stride", [1, 4])
def test_identical_nonuniform_images_still_match(stride):
    pattern, _ = images("screen")
    found = find_template(png(pattern), png(pattern), max_stride=stride)
    assert found is not None and found[:2] == (4, 4) and found[2] == pytest.approx(1)


@pytest.mark.parametrize("threshold", [0, 1])
def test_constant_exact_images_still_match_at_valid_threshold_boundaries(threshold):
    image = png(Image.new("L", (8, 8), 128))
    assert find_template(image, image, threshold=threshold) == (4, 4, 1.0)


def test_constant_images_keep_the_existing_absolute_difference_score():
    screen = png(Image.new("L", (8, 8), 129))
    template = png(Image.new("L", (8, 8), 128))
    assert find_template(screen, template) == (4, 4, 1 - 1 / 32)
