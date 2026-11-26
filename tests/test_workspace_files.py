import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "workspace_files", Path(__file__).resolve().parents[1] / "desktop" / "workspace-files.py"
)
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


def test_move_receipt_after_partial_batch_preserves_existing_files(tmp_path):
    (tmp_path / "one").write_text("one")
    (tmp_path / "two").write_text("two")
    (tmp_path / "archive").mkdir()
    fd = os.open(tmp_path, workspace.DIRECTORY)
    try:
        items = [{"source": name, "target": "archive/copy"} for name in ["one", "two"]]
        receipt = workspace.execute(fd, "move", items)
        assert receipt["one"]["moved_to"] == "archive/copy"
        assert (
            receipt["one"]["sha256"]
            == workspace.execute(fd, "verify", ["archive/copy"])["archive/copy"]
        )
        (tmp_path / "archive/copy").write_text("changed")
        assert (
            receipt["one"]["sha256"]
            != workspace.execute(fd, "verify", ["archive/copy"])["archive/copy"]
        )
        (tmp_path / "archive/copy").write_text("one")
        assert "error" in receipt["two"]
        assert workspace.execute(fd, "read", [{"source": "archive/copy"}, {"source": "two"}]) == {
            "archive/copy": "one",
            "two": "two",
        }
    finally:
        os.close(fd)


def test_traversal_symlinks_and_oversize_reads_are_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    (root / "large").write_bytes(b"x" * 24001)
    fd = os.open(root, workspace.DIRECTORY)
    try:
        for name in ["../unsafe", "/etc/passwd", "outside/root/large", "large"]:
            with pytest.raises((OSError, ValueError)):
                workspace.execute(fd, "read", [{"source": name}])
        assert workspace.execute(fd, "list", []) == ["large"]
    finally:
        os.close(fd)
