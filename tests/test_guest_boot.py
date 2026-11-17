"""Boot-file selection and saved-disk protection, without QEMU or root privileges."""

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guest(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("entrypoint.sh", "prepare-boot.sh"):
        shutil.copy2(ROOT / "vm" / name, scripts / name)
    tools = tmp_path / "bin"
    tools.mkdir()
    stub = tools / "stub"
    stub.write_text("""#!/usr/bin/env python3
import json,os,shutil,sys
from pathlib import Path
name=Path(sys.argv[0]).name;args=sys.argv[1:]
if name=='qemu-img':
    shutil.copyfile(args[-2],args[-1])
    sys.exit(int(os.environ.get('CONVERT_FAILURE','0')))
elif name=='e2fsck':sys.exit(int(os.environ.get('FILESYSTEM_FAILURE','0')))
elif name=='debugfs':
    guest=json.loads(Path(args[-1]).read_text());command=args[1].split()
    if command[0]=='ls':
        for i,version in enumerate(guest['versions']):print(f'/{i+10}/040755/0/0/{version}//')
    elif command[0]=='dump':
        if command[1] in guest['files']:Path(command[2]).write_text(guest['files'][command[1]])
        else:print('File not found by ext2_lookup',file=sys.stderr)
    else:raise RuntimeError('unexpected debugfs request')
elif name=='qemu-system-x86_64':
    record={'args':args}
    for option in ('-kernel','-initrd'):
        record[option]=Path(args[args.index(option)+1]).read_text()
    Path(os.environ['BOOT_RECORD']).write_text(json.dumps(record))
else:raise RuntimeError(name)
""")
    stub.chmod(0o755)
    for name in ("qemu-img", "e2fsck", "debugfs", "qemu-system-x86_64"):
        (tools / name).symlink_to(stub)
    disk = tmp_path / "base.qcow2"
    disk.write_text(
        json.dumps(
            {
                "versions": ["6.8.0-31-generic"],
                "files": {
                    "/boot/vmlinuz-6.8.0-31-generic": "kernel-for-saved-work",
                    "/boot/initrd.img-6.8.0-31-generic": "initrd-for-saved-work",
                    "/etc/ai-desktop-agent/cmdline": (
                        "console=ttyS0 root=/dev/vda rw guest=original"
                    ),
                },
                "user_work": "saved document",
            }
        )
    )
    # Old Compose configurations mounted these template-wide files into every VM.
    for suffix, value in (
        ("vmlinuz", "kernel-for-saved-work"),
        ("initrd.img", "initrd-for-saved-work"),
        ("cmdline.txt", "root=/dev/vda rw guest=original"),
    ):
        Path(str(disk) + "." + suffix).write_text(value)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "USE_KVM": "false",
        "ALLOW_TCG_VM": "true",
        "VM_BASE_IMAGE": str(disk),
        "VM_IMAGE": str(tmp_path / "work" / "saved.qcow2"),
        "QMP_SOCK": str(tmp_path / "qmp.sock"),
        "QGA_SOCK": str(tmp_path / "qga.sock"),
        "BOOT_RECORD": str(tmp_path / "boot.json"),
    }
    for key in ("VM_KERNEL", "VM_INITRD", "CMDLINE_FILE"):
        env.pop(key, None)

    def boot(**extra):
        return subprocess.run(
            ["bash", str(scripts / "entrypoint.sh")],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=15,
        )

    return env, boot


def test_template_rebuild_never_changes_the_saved_disks_kernel_or_work(guest):
    env, boot = guest
    result = boot()
    assert result.returncode == 0, result.stdout + result.stderr
    image = Path(env["VM_IMAGE"])
    saved = image.read_bytes()
    Path(env["VM_BASE_IMAGE"]).write_text("a completely different template")
    for suffix in ("vmlinuz", "initrd.img", "cmdline.txt"):
        Path(env["VM_BASE_IMAGE"] + "." + suffix).write_text("new-template-" + suffix)
    result = boot()
    assert result.returncode == 0, result.stdout + result.stderr
    assert image.read_bytes() == saved
    record = json.loads(Path(env["BOOT_RECORD"]).read_text())
    assert record["-kernel"] == "kernel-for-saved-work"
    assert record["-initrd"] == "initrd-for-saved-work"
    assert "guest=original" in record["args"][record["args"].index("-append") + 1]
    assert not list(image.parent.glob("*.boot.tmp.*"))


def test_existing_disk_without_boot_sidecars_is_read_without_a_template(guest):
    env, boot = guest
    image = Path(env["VM_IMAGE"])
    image.parent.mkdir()
    image.write_bytes(Path(env["VM_BASE_IMAGE"]).read_bytes())
    Path(env["VM_BASE_IMAGE"]).unlink()
    env.pop("VM_BASE_IMAGE")
    result = boot()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(Path(env["BOOT_RECORD"]).read_text())["-kernel"] == "kernel-for-saved-work"


@pytest.mark.parametrize("failure", [{"CONVERT_FAILURE": "42"}, {"FILESYSTEM_FAILURE": "8"}])
def test_failed_offline_read_preserves_saved_work_and_never_starts_qemu(guest, failure):
    env, boot = guest
    assert boot().returncode == 0
    image = Path(env["VM_IMAGE"])
    saved = image.read_bytes()
    Path(env["BOOT_RECORD"]).unlink()
    result = boot(**failure)
    assert result.returncode != 0
    assert image.read_bytes() == saved
    assert not Path(env["BOOT_RECORD"]).exists()
    assert not list(image.parent.glob("*.boot.tmp.*"))


def test_partial_newer_kernel_is_skipped_and_legacy_cmdline_uses_the_known_root(guest):
    env, boot = guest
    base = Path(env["VM_BASE_IMAGE"])
    data = json.loads(base.read_text())
    data["versions"].append("6.10.0-1-generic")
    data["files"]["/boot/vmlinuz-6.10.0-1-generic"] = "incomplete upgrade"
    del data["files"]["/etc/ai-desktop-agent/cmdline"]
    base.write_text(json.dumps(data))
    result = boot()
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(Path(env["BOOT_RECORD"]).read_text())
    assert record["-kernel"] == "kernel-for-saved-work"
    assert record["-initrd"] == "initrd-for-saved-work"
    assert "root=/dev/vda" in record["args"][record["args"].index("-append") + 1]


def test_no_matching_modules_and_boot_files_is_an_error_without_overwriting_work(guest):
    env, boot = guest
    base = Path(env["VM_BASE_IMAGE"])
    data = json.loads(base.read_text())
    data["versions"] = ["a-kernel-without-boot-files"]
    base.write_text(json.dumps(data))
    result = boot()
    assert result.returncode != 0
    assert "no kernel and initrd matching" in result.stderr
    assert Path(env["VM_IMAGE"]).read_bytes() == base.read_bytes()
    assert not Path(env["BOOT_RECORD"]).exists()


def test_a_live_disk_lock_rejects_startup_before_copying_or_reading_the_guest(guest):
    env, boot = guest
    image = Path(env["VM_IMAGE"])
    image.parent.mkdir()
    with Path(str(image) + ".run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = boot()
    assert result.returncode != 0 and "already starting or running" in result.stdout
    assert not image.exists()
    assert not Path(env["BOOT_RECORD"]).exists()


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_a_template_alias_is_never_opened_as_a_writable_instance(guest, alias):
    env, boot = guest
    image, base = Path(env["VM_IMAGE"]), Path(env["VM_BASE_IMAGE"])
    image.parent.mkdir()
    saved = base.read_bytes()
    if alias == "symlink":
        image.symlink_to(base)
    else:
        image.hardlink_to(base)
    result = boot()
    assert result.returncode != 0
    assert "separate from VM_IMAGE" in result.stdout
    assert base.read_bytes() == saved
    assert not Path(env["BOOT_RECORD"]).exists()


def test_a_saved_disk_still_starts_when_the_template_has_been_removed(guest):
    env, boot = guest
    assert boot().returncode == 0
    Path(env["VM_BASE_IMAGE"]).unlink()
    result = boot()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(Path(env["BOOT_RECORD"]).read_text())["-kernel"] == "kernel-for-saved-work"
