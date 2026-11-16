import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def select(tmp_path, mode="auto", arch="x86_64", kvm=True, prepared=True):
    device = tmp_path / "kvm"
    if kvm:
        device.touch()
    vm = tmp_path / "vm"
    vm.mkdir(exist_ok=True)
    if prepared:
        for name in ("desktop.qcow2", "vmlinuz", "initrd.img", "cmdline.txt"):
            (vm / name).write_text("prepared")
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; choose_profile "$2" "$3" "$4"',
            "bash",
            str(ROOT / "scripts/up.sh"),
            mode,
            str(device),
            arch,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "mode,arch,kvm,prepared,profile",
    [
        ("auto", "x86_64", True, True, "kvm"),
        ("auto", "x86_64", False, True, "container"),
        ("auto", "aarch64", True, True, "container"),
        ("auto", "x86_64", True, False, "container"),
        ("false", "x86_64", True, True, "container"),
    ],
)
def test_startup_chooses_a_usable_environment(tmp_path, mode, arch, kvm, prepared, profile):
    result = select(tmp_path, mode, arch, kvm, prepared)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == profile


@pytest.mark.parametrize(
    "arch,kvm,prepared", [("aarch64", True, True), ("x86_64", False, True), ("x86_64", True, False)]
)
def test_explicit_kvm_never_silently_starts_an_unusable_guest(tmp_path, arch, kvm, prepared):
    result = select(tmp_path, "true", arch, kvm, prepared)
    assert result.returncode != 0
    assert "USE_KVM=false" in result.stderr


def test_startup_uses_compose_configuration_and_does_not_print_other_settings(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    startup = scripts / "up.sh"
    startup.write_bytes((ROOT / "scripts/up.sh").read_bytes())
    (tmp_path / ".env").write_text('USE_KVM="false"\n')
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
if sys.argv[1:]==['compose','config','--environment']:
    print('USE_KVM=false\\nOPENAI_API_KEY=never-print-this-value')
else:
    Path(os.environ['STARTUP_RECORD']).write_text(json.dumps({'args':sys.argv[1:],'vnc':os.environ.get('VNC_HOST'),'kvm':os.environ.get('USE_KVM')}))
""")
    docker.chmod(0o755)
    output = tmp_path / "record.json"
    result = subprocess.run(
        ["bash", str(startup)],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "STARTUP_RECORD": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "never-print-this-value" not in result.stdout + result.stderr
    record = json.loads(output.read_text())
    assert record["vnc"] == "desktop" and record["kvm"] == "false"
    assert record["args"][-3:] == ["desktop", "backend", "frontend"]
    assert "container" in record["args"]


def guest_env(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    qemu = tools / "qemu-system-x86_64"
    qemu.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ['QEMU_RECORD']).write_text(json.dumps(sys.argv[1:]))
""")
    qemu.chmod(0o755)
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"original-template")
    for name in ("kernel", "initrd", "cmdline"):
        (tmp_path / name).write_text("prepared")
    return {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "USE_KVM": "false",
        "ALLOW_TCG_VM": "true",
        "VM_BASE_IMAGE": str(base),
        "VM_IMAGE": str(tmp_path / "work/default.qcow2"),
        "VM_KERNEL": str(tmp_path / "kernel"),
        "VM_INITRD": str(tmp_path / "initrd"),
        "CMDLINE_FILE": str(tmp_path / "cmdline"),
        "QMP_SOCK": str(tmp_path / "qmp.sock"),
        "QGA_SOCK": str(tmp_path / "qga.sock"),
        "QEMU_RECORD": str(tmp_path / "qemu.json"),
    }


def boot(env):
    return subprocess.run(
        ["bash", str(ROOT / "vm/entrypoint.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_fixed_guest_uses_private_disk_and_preserves_work_on_restart(tmp_path):
    env = guest_env(tmp_path)
    assert boot(env).returncode == 0
    work = Path(env["VM_IMAGE"])
    assert work.read_bytes() == b"original-template"
    work.write_bytes(b"saved-work")
    assert boot(env).returncode == 0
    assert work.read_bytes() == b"saved-work"
    assert Path(env["VM_BASE_IMAGE"]).read_bytes() == b"original-template"
    args = json.loads(Path(env["QEMU_RECORD"]).read_text())
    assert f"file={work},if=virtio,format=qcow2" in args
    assert env["VM_KERNEL"] in args and env["VM_INITRD"] in args


def test_failed_initial_copy_cannot_publish_a_partial_disk(tmp_path):
    env = guest_env(tmp_path)
    cp = tmp_path / "bin/cp"
    cp.write_text('#!/bin/bash\nprintf partial > "${@: -1}"\nexit 1\n')
    cp.chmod(0o755)
    assert boot(env).returncode != 0
    assert not Path(env["VM_IMAGE"]).exists()
    assert not list((tmp_path / "work").glob("*.??????"))
    cp.unlink()
    assert boot(env).returncode == 0
    assert Path(env["VM_IMAGE"]).read_bytes() == b"original-template"


def test_invalid_guest_files_fail_before_qemu_runs(tmp_path):
    env = guest_env(tmp_path)
    Path(env["VM_KERNEL"]).unlink()
    result = boot(env)
    assert result.returncode != 0
    assert "missing or invalid VM file" in result.stdout
    assert not Path(env["QEMU_RECORD"]).exists()
