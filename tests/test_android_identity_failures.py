"""Unknown persistent data must not be treated as a newly created Android device."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.persistence import StorageError


def provisioner(tmp_path, monkeypatch, content):
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    image = tmp_path / "android" / "android-data.img"
    image.parent.mkdir()
    if content is not None:
        image.write_bytes(content)
    state = tmp_path / "state.json"
    original = '{"android":{"done":true,"data_id":"ext4:original-data","at":1}}'
    state.write_text(original)
    pool = Mock()
    pool.instance_id.return_value = "same-physical-container"
    return AndroidProvisioner(pool=pool, state_file=state), pool, image, state, original


@pytest.mark.parametrize("failure", ["unreadable", "invalid-header", "truncated", "zero-uuid"])
def test_an_existing_unreadable_or_corrupt_disk_never_falls_back_to_container_identity(
    tmp_path, monkeypatch, failure
):
    block = bytearray(0x78)
    if failure == "zero-uuid":
        block[0x38:0x3A] = b"\x53\xef"
    p, pool, image, state, original = provisioner(tmp_path, monkeypatch, b"\0" * 1024 + block)
    if failure == "truncated":
        image.write_bytes(b"truncated data")
    if failure == "unreadable":
        open_file = Path.open

        def open_checked(path, *args, **kwargs):
            if path == image:
                raise PermissionError("data image cannot be read")
            return open_file(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_checked)
    with pytest.raises(StorageError):
        p.needs("android")
    with pytest.raises(StorageError):
        p.mark_done("android")
    pool.instance_id.assert_not_called()
    assert state.read_text() == original


def test_a_genuinely_absent_legacy_disk_still_uses_the_container_identity(tmp_path, monkeypatch):
    p, pool, _, state, _ = provisioner(tmp_path, monkeypatch, None)
    assert p.needs("android") is True
    p.mark_done("android")
    assert p.needs("android") is False
    assert (
        json.loads(state.read_text())["android"]["data_id"] == "container:same-physical-container"
    )
    assert pool.instance_id.call_count == 3


def test_a_dynamic_android_environment_does_not_consult_the_fixed_data_image(tmp_path, monkeypatch):
    p, pool, _, state, _ = provisioner(tmp_path, monkeypatch, b"broken fixed image")
    p.mark_done("and-dynamic")
    assert p.needs("and-dynamic") is False
    assert (
        json.loads(state.read_text())["and-dynamic"]["data_id"]
        == "container:same-physical-container"
    )
    assert pool.instance_id.call_count == 2
