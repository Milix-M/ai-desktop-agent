"""Real Firefox pixels do not provide a match when only one image has variation."""

import hashlib
import io
import json
import os
import subprocess
import tarfile

import pytest
from PIL import Image

from ai_desktop_agent.vm.matcher import find_template
from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]


def png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(scope="module")
def rendered_page(desktop):
    html = (
        "<title>Template boundary pixels</title>"
        "<style>html,body{margin:0;background:rgb(128,128,128)}"
        "div{position:absolute;left:1px;top:1px;width:1px;height:1px;"
        "background:rgb(129,129,129)}</style><div></div>"
    )
    result = desktop.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; Path('/tmp/template-boundary-profile').mkdir(exist_ok=True);"
            "Path('/tmp/template-boundary.html').write_text(" + repr(html) + ")",
        ],
        user="agent",
    )
    assert result.exit_code == 0, result.output.decode()
    result = subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "agent",
            desktop.id,
            "firefox",
            "--headless",
            "--no-remote",
            "--profile",
            "/tmp/template-boundary-profile",
            "--window-size",
            "64,64",
            "--screenshot",
            "/tmp/template-boundary.png",
            "file:///tmp/template-boundary.html",
        ],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    chunks, _ = desktop.get_archive("/tmp/template-boundary.png")
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
        file = archive.extractfile("template-boundary.png")
        assert file is not None
        return file.read()


@pytest.mark.parametrize("varying", ["screen", "template"])
def test_browser_rendered_pixels_cannot_authorize_an_undefined_correlation(
    rendered_page, tmp_path, varying
):
    with Image.open(io.BytesIO(rendered_page)) as image:
        pattern = image.crop((0, 0, 8, 8)).convert("L")
        flat = image.crop((16, 16, 24, 24)).convert("L")
    assert pattern.histogram()[128] == 63 and pattern.getpixel((1, 1)) == 129
    assert flat.getextrema() == (128, 128)
    patterned, uniform = png(pattern), png(flat)
    (tmp_path / "rendered.png").write_bytes(rendered_page)
    (tmp_path / "pattern.png").write_bytes(patterned)
    (tmp_path / "uniform.png").write_bytes(uniform)
    screen, template = (patterned, uniform) if varying == "screen" else (uniform, patterned)
    proof = {
        "varying": varying,
        "renderer": "firefox-headless",
        "frame_sha256": hashlib.sha256(rendered_page).hexdigest(),
        "pattern_sha256": hashlib.sha256(patterned).hexdigest(),
        "uniform_sha256": hashlib.sha256(uniform).hexdigest(),
        "result": find_template(screen, template),
        "identical": find_template(patterned, patterned),
        "constant": find_template(uniform, uniform),
    }
    try:
        find_template(screen, template, threshold=float("nan"))
    except Exception as error:
        proof["nan_error"] = type(error).__name__
    (tmp_path / "template-boundary-proof.json").write_text(json.dumps(proof, indent=2))
    assert proof["result"] is None, proof
    assert proof["identical"] == (4, 4, 1.0) and proof["constant"] == (4, 4, 1.0), proof
    assert proof.get("nan_error") == "ValueError", proof
