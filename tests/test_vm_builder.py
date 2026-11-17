import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder(tmp_path):
    output = tmp_path / "vm"
    output.mkdir()
    script = output / "build-vm-image.sh"
    # Localize the old fixed path as well, so a regression never touches host /tmp data.
    text = (
        (ROOT / "vm/build-vm-image.sh")
        .read_text()
        .replace('TARGET="/tmp/vm-rootfs-build"', f'TARGET="{tmp_path}/old-rootfs"')
    )
    script.write_text(text)
    tools = tmp_path / "tools"
    tools.mkdir()
    stub = tools / "stub"
    stub.write_text("""#!/usr/bin/env python3
import json,os,shutil,signal,sys
from pathlib import Path
name=Path(sys.argv[0]).name;args=sys.argv[1:]
base=Path(os.environ['BUILD_TEST_ROOT']).resolve()
def event(kind,**data):
    with (base/'events').open('a') as out:out.write(json.dumps({'kind':kind,**data})+'\\n')
def safe(path):
    p=Path(path).absolute()
    if not p.resolve().is_relative_to(base):raise RuntimeError('test sandbox escape: '+str(p))
    return p
state=base/'mounts.json'
mounted=json.loads(state.read_text()) if state.exists() else []
failure=os.environ.get('BUILD_FAILURE','')
if name=='id':print(os.environ.get('BUILD_UID','0'));sys.exit()
if name=='uname':print('x86_64');sys.exit()
if name=='debootstrap':
    target=safe(args[args.index('noble')+1]);event('bootstrap',target=str(target))
    for p in ('etc/apt','etc/systemd/system','proc','sys','dev','usr/share/xsessions',
              'lib/modules/test','boot','var/lib/apt/lists','var/cache/apt/archives','etc/sudoers.d'):
        (target/p).mkdir(parents=True,exist_ok=True)
    for p in ('usr/share/xsessions/plasma.desktop','boot/vmlinuz-test','boot/initrd.img-test'):
        (target/p).write_text('new-'+p)
    (target/'sentinel').write_text('preserve mounted root')
    if failure=='bootstrap':sys.exit(22)
    if failure=='interrupt':os.kill(os.getppid(),signal.SIGTERM)
elif name=='chroot':sys.exit()
elif name=='mount':
    target=str(safe(args[-1]));event('mount',target=target)
    if failure=='mount' and target.endswith('/sys'):sys.exit(37)
    mounted.append(target);state.write_text(json.dumps(mounted))
elif name=='mountpoint':
    if failure=='mount-probe':sys.exit(1)
    sys.exit(0 if args[-1] in mounted else 32)
elif name=='umount':
    target=args[-1];event('unmount',target=target)
    if failure=='mount' and target.endswith('/proc'):sys.exit(1)
    if target in mounted:mounted.remove(target)
    state.write_text(json.dumps(mounted))
elif name=='rm':
    targets=[safe(p) for p in args if not p.startswith('-')]
    event('remove',targets=[str(p) for p in targets],mounted=mounted)
    if any(Path(m).is_relative_to(p) for m in mounted for p in targets):sys.exit(99)
    for p in targets:
        if p.is_dir():shutil.rmtree(p)
        else:p.unlink(missing_ok=True)
elif name=='truncate':safe(args[-1]).touch()
elif name=='mkfs.ext4':safe(args[-1]).write_text('ext4')
elif name=='qemu-img':
    if failure=='image':sys.exit(41)
    safe(args[-1]).write_text('new-image')
elif name=='cp':
    source,target=safe(args[-2]),safe(args[-1])
    if '--link' in args:
        if failure=='backup':sys.exit(45)
        os.link(source,target,follow_symlinks=False)
    else:shutil.copyfile(source,target)
elif name=='mv':
    source,target=safe(args[-2]),safe(args[-1])
    event('move',source=str(source),target=str(target))
    output=Path(os.environ['VM_OUTPUT_DIR'])
    failed=base/'failed-move'
    if (target.parent==output and target.name=='initrd.img'
        and failure.startswith('publish') and not failed.exists()):
        failed.touch();sys.exit(43)
    if (failure=='publish-rollback' and source.parent.name=='previous'
        and target.name=='desktop.qcow2'):sys.exit(44)
    os.replace(source,target)
    if failure=='publish-interrupt' and target.parent==output and not failed.exists():
        failed.touch();os.kill(os.getppid(),signal.SIGTERM)
else:raise RuntimeError('unexpected test command: '+name)
""")
    stub.chmod(0o755)
    for name in (
        "id",
        "uname",
        "apt-get",
        "update-binfmts",
        "debootstrap",
        "chroot",
        "mount",
        "mountpoint",
        "umount",
        "rm",
        "truncate",
        "mkfs.ext4",
        "qemu-img",
        "cp",
        "mv",
    ):
        (tools / name).symlink_to(stub)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "BUILD_TEST_ROOT": str(tmp_path),
        "TMPDIR": str(temporary),
        "VM_OUTPUT_DIR": str(output),
    }

    def run(**extra):
        result = subprocess.run(
            ["bash", str(script)], env={**env, **extra}, capture_output=True, text=True, timeout=10
        )
        events = (
            [json.loads(line) for line in (tmp_path / "events").read_text().splitlines()]
            if (tmp_path / "events").exists()
            else []
        )
        return result, events

    return output, run


def test_non_root_rejection_does_not_attempt_cleanup(builder):
    _, run = builder
    result, events = run(BUILD_UID="1001")
    assert result.returncode != 0
    assert not events


def test_unmount_failure_preserves_the_worktree_and_never_attempts_recursive_delete(builder):
    _, run = builder
    result, events = run(BUILD_FAILURE="mount")
    assert result.returncode != 0
    assert not [e for e in events if e["kind"] == "remove" and e["mounted"]]
    target = Path(next(e["target"] for e in events if e["kind"] == "bootstrap"))
    assert (target / "sentinel").is_file()
    assert "preserv" in (result.stdout + result.stderr).lower()


def test_failed_image_conversion_keeps_the_complete_previous_guest_files(builder):
    output, run = builder
    previous = {
        name: "old-" + name for name in ("desktop.qcow2", "vmlinuz", "initrd.img", "cmdline.txt")
    }
    for name, body in previous.items():
        (output / name).write_text(body)
    result, _ = run(BUILD_FAILURE="image")
    assert result.returncode == 41, result.stdout + result.stderr
    assert {name: (output / name).read_text() for name in previous} == previous


def test_failed_bootstrap_uses_a_unique_private_worktree_for_each_attempt(builder):
    _, run = builder
    result, events = run(BUILD_FAILURE="bootstrap")
    assert result.returncode != 0
    first = next(e["target"] for e in events if e["kind"] == "bootstrap")
    result, events = run(BUILD_FAILURE="bootstrap")
    assert result.returncode != 0
    paths = [e["target"] for e in events if e["kind"] == "bootstrap"]
    assert first != paths[-1]


@pytest.mark.parametrize(
    "failure,code",
    [("publish", 43), ("publish-interrupt", 143), ("backup", 45), ("interrupt", 143)],
)
def test_publication_errors_and_signals_preserve_the_previous_guest_set(builder, failure, code):
    output, run = builder
    previous = {
        name: "old-" + name for name in ("desktop.qcow2", "vmlinuz", "initrd.img", "cmdline.txt")
    }
    for name, body in previous.items():
        (output / name).write_text(body)
    result, _ = run(BUILD_FAILURE=failure)
    assert result.returncode == code, result.stdout + result.stderr
    assert {name: (output / name).read_text() for name in previous} == previous
    assert not list(output.glob(".vm-build.*"))


def test_failed_rollback_retains_the_previous_files_for_manual_recovery(builder):
    output, run = builder
    for name in ("desktop.qcow2", "vmlinuz", "initrd.img", "cmdline.txt"):
        (output / name).write_text("old-" + name)
    result, _ = run(BUILD_FAILURE="publish-rollback")
    assert result.returncode != 0
    assert "preserving" in result.stderr
    work = next(output.glob(".vm-build.*"))
    assert (work / "previous/desktop.qcow2").read_text() == "old-desktop.qcow2"


@pytest.mark.parametrize("previous", [False, True])
def test_success_publishes_the_complete_guest_set_and_removes_only_its_worktree(builder, previous):
    output, run = builder
    if previous:
        for name in ("desktop.qcow2", "vmlinuz", "initrd.img", "cmdline.txt"):
            (output / name).write_text("old-" + name)
    unrelated = output / "unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel").write_text("keep")
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (output / "desktop.qcow2").read_text() == "new-image"
    assert (output / "vmlinuz").read_text() == "new-boot/vmlinuz-test"
    assert (output / "initrd.img").read_text() == "new-boot/initrd.img-test"
    assert "root=/dev/vda" in (output / "cmdline.txt").read_text()
    assert (unrelated / "sentinel").read_text() == "keep"
    assert not list(output.glob(".vm-build.*"))


def test_a_build_lock_rejects_another_builder_before_bootstrap(builder):
    output, run = builder
    with (output / ".vm-image-build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result, events = run()
    assert result.returncode != 0 and "another VM image build" in result.stderr
    assert not events


def test_output_directories_are_rejected_without_overwriting_or_bootstrapping(builder):
    output, run = builder
    (output / "vmlinuz").mkdir()
    (output / "vmlinuz/sentinel").write_text("keep")
    result, events = run()
    assert result.returncode != 0 and not events
    assert (output / "vmlinuz/sentinel").read_text() == "keep"


def test_an_unreadable_mount_state_is_preserved_instead_of_treated_as_unmounted(builder):
    output, run = builder
    result, _ = run(BUILD_FAILURE="mount-probe")
    assert result.returncode != 0
    assert "preserving" in result.stderr
    work = next(output.glob(".vm-build.*"))
    assert (work / "rootfs/sentinel").is_file()


@pytest.mark.parametrize("failure,symlink", [(False, False), (True, False), (True, True)])
def test_real_file_publication_and_rollback_preserve_previous_inodes(tmp_path, failure, symlink):
    script = ROOT / "vm/build-vm-image.sh"
    assert 'if [[ "${BASH_SOURCE[0]}" == "$0" ]]' in script.read_text()
    output = tmp_path / "output"
    output.mkdir()
    names = ("desktop.qcow2", "vmlinuz", "initrd.img", "cmdline.txt")
    for name in names:
        (output / name).write_text("old-" + name)
    if symlink:
        (output / "vmlinuz").rename(output / "old-kernel")
        (output / "vmlinuz").symlink_to("old-kernel")
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1"
OUTPUT_DIR="$2"
WORK_DIR=$(mktemp -d "$OUTPUT_DIR/.vm-build.XXXXXXXX")
TARGET="$WORK_DIR/rootfs"
STAGING="$WORK_DIR/output"
mkdir -p "$TARGET/dev" "$TARGET/sys" "$TARGET/proc" "$STAGING"
trap cleanup EXIT
for name in desktop.qcow2 vmlinuz initrd.img cmdline.txt; do
    if [ "$3" = true ] && [ "$name" = initrd.img ]; then continue; fi
    echo -n "new-$name" > "$STAGING/$name"
done
publish_artifacts
""",
            "bash",
            str(script),
            str(output),
            str(failure).lower(),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode != 0) == failure, result.stdout + result.stderr
    prefix = "old-" if failure else "new-"
    assert {name: (output / name).read_text() for name in names} == {
        name: prefix + name for name in names
    }
    if symlink:
        assert (output / "vmlinuz").is_symlink()
        assert (output / "old-kernel").read_text() == "old-vmlinuz"
    assert not list(output.glob(".vm-build.*"))
