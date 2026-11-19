"""Nonempty crops and per-axis geometry survive quantized resizing."""

import io
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot


def frame(width=1280, height=1280):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (31, 79, 113)).save(buffer, format="PNG")
    return Screenshot(buffer.getvalue(), width, height, timestamp=12.5, frame_number=7)


@pytest.mark.parametrize("width,height,expected", [(1280, 1, (1024, 1)), (1, 1280, (1, 1024))])
def test_nonempty_thin_regions_remain_decodable_through_both_existing_crop_apis(
    width, height, expected
):
    original = frame()
    before = asdict(original)
    for crop in [
        original.crop(0, 0, width, height),
        original.crop_with_meta(0, 0, width, height)[0],
    ]:
        decoded = crop.to_pil()
        assert decoded.size == expected == (crop.width, crop.height)
        assert decoded.getpixel((0, 0)) == (31, 79, 113)
        assert crop.timestamp == 12.5 and crop.frame_number == 7
    assert asdict(original) == before


def test_a_positive_subpixel_scale_still_retains_one_pixel_and_a_usable_horizontal_scale():
    cropped, (_, _, scale) = frame(1, 1).crop_with_meta(0, 0, 1, 1, scale=0.1)
    assert cropped.to_pil().size == (1, 1)
    assert scale == 1.0


@pytest.mark.parametrize(
    "options",
    [
        {"scale": 0},
        {"scale": -1},
        {"scale": float("nan")},
        {"scale": float("inf")},
        {"scale": True},
        {"scale": 10**1000},
        {"scale": 1e308},
        {"max_dim": 0},
        {"max_dim": -1},
        {"max_dim": True},
        {"max_dim": 1.5},
    ],
    ids=[
        "zero-scale",
        "negative-scale",
        "nan-scale",
        "infinite-scale",
        "boolean-scale",
        "huge-scale",
        "overflowing-dimensions",
        "zero-limit",
        "negative-limit",
        "boolean-limit",
        "fractional-limit",
    ],
)
def test_invalid_resizing_options_are_rejected_before_producing_an_image(options):
    original = frame(10, 10)
    with pytest.raises(ValueError, match="scale|max_dim"):
        original.crop(0, 0, 10, 10, **options)
    assert original.to_pil().size == (10, 10)


@pytest.mark.parametrize(
    "width,height,limit,expected,scales",
    [
        (3, 500, 256, (1, 256), (1 / 3, 256 / 500)),
        (500, 3, 256, (256, 1), (256 / 500, 1 / 3)),
        (3, 250, 256, (3, 256), (1.0, 256 / 250)),
        (600, 600, 1024, (1024, 1024), (1024 / 600, 1024 / 600)),
    ],
)
def test_geometry_reports_the_actual_scale_of_each_decoded_axis(
    width, height, limit, expected, scales
):
    original = frame()
    cropped, geometry = original.crop_with_geometry(0, 0, width, height, max_dim=limit)
    assert cropped.to_pil().size == expected
    assert geometry.origin_x == geometry.origin_y == 0
    assert geometry.scale_x == pytest.approx(scales[0])
    assert geometry.scale_y == pytest.approx(scales[1])
    assert 0 <= int((cropped.width - 1) / geometry.scale_x) < width
    assert 0 <= int((cropped.height - 1) / geometry.scale_y) < height


def test_geometry_uses_the_decoded_crop_even_when_capture_metadata_is_stale():
    image = frame()
    original = Screenshot(image.image_bytes, 17, 19)
    cropped, geometry = original.crop_with_geometry(1277, 80, 50, 500, max_dim=256)
    assert cropped.to_pil().size == (1, 256)
    assert (geometry.origin_x, geometry.origin_y) == (1277, 80)
    assert geometry.scale_x == pytest.approx(1 / 3)
    assert geometry.scale_y == pytest.approx(256 / 500)


class LimitedScreenshot(Screenshot):
    def crop_with_meta(self, *args, **kwargs):
        return super().crop_with_meta(*args, **(kwargs | {"max_dim": 256}))


def decision(kind, **params):
    return ActionDecision(Action(kind, params), "inspect report", 1.0, "visible target")


@pytest.mark.parametrize("operation", ["move", "drag", "nested"])
async def test_limited_adapter_keeps_both_absolute_axes_correct_for_real_resized_pixels(operation):
    initial = decision(ActionType.REGION_SELECT, x=1277, y=80, width=3, height=500)
    refined = (
        decision(ActionType.DRAG, start_x=0, start_y=64, end_x=0, end_y=192)
        if operation == "drag"
        else decision(ActionType.MOUSE_MOVE, x=0, y=128)
    )
    replies = [initial, refined]
    if operation == "nested":
        replies.insert(1, decision(ActionType.REGION_SELECT, x=0, y=64, width=1, height=128))
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(side_effect=replies)
    display = FakeDisplayBackend(screen_width=1280, screen_height=800)
    raw = LimitedScreenshot(**asdict(frame(1280, 800)))
    display.capture_raw = lambda: raw
    session = TaskSession(llm=model, display=display, decision=None)
    session.executor.wait_for_still = AsyncMock()
    session.loop.start(Goal("確認する報告の位置へ移動する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("point", "報告の位置へ移動する")])
    try:
        await session._execute_phase()
        if operation == "drag":
            assert display.drags == [{"start": (1277, 205), "end": (1277, 455), "button": 1}]
        else:
            assert display.mouse_moves == [(1277, 330)]
        assert session.loop.state == AgentState.VERIFYING
        assert session.loop.context.action_history[-1].success
        assert model.decide_next_action.await_count == len(replies)
        images = [
            call.kwargs["screenshot"].to_pil().size
            for call in model.decide_next_action.call_args_list[1:]
        ]
        assert images == ([(1, 256), (3, 256)] if operation == "nested" else [(1, 256)])
    finally:
        await session.aclose()


def test_a_cursor_overlay_without_grid_exports_a_decodable_cropped_png_data_url():
    from urllib.request import urlopen

    source = frame(120, 100)
    marked = source.with_overlay(cursor_x=60, cursor_y=50, show_grid=False, grid_spacing=10)
    with marked.to_pil() as overlay:
        assert overlay.getpixel((30, 40)) == (31, 79, 113)
        assert overlay.getpixel((60, 50)) != (31, 79, 113)
    crop, geometry = marked.crop_with_geometry(20, 20, 70, 60, scale=2)
    with urlopen(crop.image_data_url) as exported:
        assert exported.headers.get_content_type() == "image/png"
        payload = exported.read()
    assert payload == crop.image_bytes and len(payload) == crop.size_bytes
    with Image.open(io.BytesIO(payload)) as decoded, crop.to_pil() as original:
        assert decoded.format == "PNG" and decoded.size == (140, 120)
        assert decoded.tobytes() == original.tobytes()
    assert (geometry.origin_x, geometry.origin_y, geometry.scale_x, geometry.scale_y) == (
        20,
        20,
        2,
        2,
    )
    assert crop.timestamp == source.timestamp and crop.frame_number == source.frame_number
    with source.to_pil() as unchanged:
        assert unchanged.getpixel((60, 50)) == (31, 79, 113)
