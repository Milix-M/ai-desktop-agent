"""Real copied disks and runtime mounts agree with the backend's filesystem view."""

import io
import json
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, VmPool

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

PROBE = """
import json,sys,time
from pathlib import Path
from unittest.mock import patch
import docker
from ai_desktop_agent.server.vm_pool import VmPool
root,image,allowed,kind=sys.argv[1:]
allowed=set(json.loads(allowed))
client=docker.from_env(timeout=15)
pool=VmPool(client=client,state_file='/app/data/paths.json')
attempts=[]
launch=type(client.containers).run
def checked(collection,image,*args,**kwargs):
    sources=list(kwargs.get('volumes',{}))
    attempts.append(sources)
    safe=lambda source:source in allowed or any(source.startswith(base+'/') for base in allowed)
    if not all(safe(source) for source in sources):
        raise RuntimeError('test blocked an unexpected host source before Docker could create it')
    return launch(collection,image,*args,**kwargs)
proof={}
try:
    with patch.object(type(client.containers),'run',checked):
        result=pool.create_vm(kind=kind)
    runtime=client.containers.get(result.vnc_host)
    for _ in range(100):
        out=runtime.exec_run(['cat','/tmp/disk-proof'])
        if out.exit_code==0:
            proof['contents']=out.output.decode();break
        time.sleep(.05)
    runtime.reload()
    proof['mounts']=runtime.attrs['Mounts']
    proof['id']=result.id
except Exception as error:
    proof.update(error_type=type(error).__name__,error=str(error))
finally:
    proof['attempts']=attempts
    state=Path('/app/data/paths.json')
    proof['state']=json.loads(state.read_text()) if state.exists() else None
    pool.close();client.close()
print(json.dumps(proof))
"""


@pytest.mark.parametrize(
    "layout", ["local", "separate-data", "named-data", "split-mounts", "android"]
)
def test_a_copied_disk_and_runtime_use_the_same_owned_host_files(tmp_path, monkeypatch, layout):
    client = docker.from_env(timeout=15)
    project = "host-paths-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    monkeypatch.setenv("USE_KVM", "false")
    monkeypatch.delenv("VM_HOST_DIR", raising=False)
    base = client.images.get(
        os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    ).id
    runtime_image, control, volume = None, None, None
    disks = tmp_path / "actual-vm"
    disks.mkdir()
    (disks / "overlays").mkdir()
    (disks / "sockets").mkdir()
    contents = "the selected template; preserve this disk"
    (disks / "desktop.qcow2").write_text(contents)
    (tmp_path / "metadata").mkdir()
    # A wrong inferred repository can silently find a different valid-looking disk.
    (tmp_path / "vm/overlays").mkdir(parents=True)
    (tmp_path / "vm/desktop.qcow2").write_text("wrong template")
    try:
        cmd = [
            "/app/.venv/bin/python",
            "-c",
            "from pathlib import Path;import os,time;"
            "pending=Path('/tmp/disk-proof.pending');"
            "pending.write_bytes(Path(os.environ.get('VM_IMAGE') or "
            "os.environ['ANDROID_ISO']).read_bytes());"
            "pending.replace('/tmp/disk-proof');time.sleep(180)",
        ]
        dockerfile = (
            f"FROM {base}\nLABEL {LABEL_PROJECT}={project}\n"
            "RUN printf 'exit 0\\n' > /prepare-boot.sh\n"
            f"CMD {json.dumps(cmd)}\n"
        )
        built, _ = client.images.build(
            fileobj=io.BytesIO(dockerfile.encode()),
            tag="ai-desktop-audit-01a1ea83-" + project,
            rm=True,
            pull=False,
        )
        runtime_image = built.id
        assert runtime_image != base
        monkeypatch.setenv("VM_IMAGE_REF", runtime_image)
        if layout == "local":
            # Local layout uses the actual repository tree and an unprivileged owner.
            monkeypatch.chdir(tmp_path)
            (tmp_path / "vm/desktop.qcow2").write_text(contents)
            pool = VmPool(
                client=client,
                state_file=tmp_path / "metadata/paths.json",
                overlays_dir=str(tmp_path / "vm/overlays"),
            )
            proof = {}
            try:
                vm_id = "vm-local-copy"
                copied = pool._create_overlay(vm_id, image=runtime_image)
                runtime = client.containers.run(
                    runtime_image,
                    detach=True,
                    network_mode="none",
                    labels=labels,
                    volumes={str(Path(copied).parent): {"bind": "/vm/instance", "mode": "rw"}},
                    environment={"VM_IMAGE": "/vm/instance/disk.qcow2"},
                )
                for _ in range(100):
                    out = runtime.exec_run(["cat", "/tmp/disk-proof"])
                    if out.exit_code == 0:
                        proof["contents"] = out.output.decode()
                        break
                    time.sleep(0.05)
                runtime.reload()
                proof.update(id=vm_id, mounts=runtime.attrs["Mounts"])
            except Exception as error:
                proof.update(
                    error_type=type(error).__name__, error=str(error), cause=str(error.__cause__)
                )
            finally:
                pool.close()
            expected = tmp_path / "vm/overlays"
        else:
            data = str(tmp_path / "metadata")
            if layout == "named-data":
                volume = client.volumes.create(project + "-metadata", labels=labels)
                data = volume.name
                client.containers.run(
                    base,
                    entrypoint=["python3", "-c"],
                    command=[f"import os;os.chown('/owned-data',{os.getuid()},{os.getgid()})"],
                    network_mode="none",
                    cap_drop=["ALL"],
                    cap_add=["CHOWN"],
                    remove=True,
                    volumes={volume.name: {"bind": "/owned-data", "mode": "rw"}},
                    labels=labels,
                )
            mounts = {
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                data: {"bind": "/app/data", "mode": "rw"},
            }
            if layout == "android":
                media = tmp_path / "phone-media"
                media.mkdir()
                (media / "boot.iso").write_text(contents)
                (media / "kernel").write_text("kernel")
                (media / "initrd.img").write_text("initrd")
                mounts[str(media)] = {"bind": "/app/android-host", "mode": "ro"}
            elif layout == "split-mounts":
                mounts.update(
                    {
                        str(disks / "overlays"): {"bind": "/vm/overlays", "mode": "rw"},
                        str(disks / "sockets"): {"bind": "/vm/sockets", "mode": "rw"},
                        str(disks / "desktop.qcow2"): {"bind": "/vm/desktop.qcow2", "mode": "ro"},
                    }
                )
            else:
                mounts[str(disks)] = {"bind": "/vm", "mode": "rw"}
            management = client.networks.create(project + "_default", labels=labels)
            control = client.containers.run(
                base,
                name=project + "-backend",
                detach=True,
                network=management.name,
                networking_config={management.name: {"GwPriority": 1}},
                cap_drop=["NET_RAW"],
                user=f"{os.getuid()}:{os.getgid()}",
                group_add=[str(os.stat("/var/run/docker.sock").st_gid)],
                entrypoint=["/app/.venv/bin/python", "-c"],
                command=["import time;time.sleep(180)"],
                labels={**labels, "com.docker.compose.service": "backend"},
                volumes=mounts,
                environment={
                    "COMPOSE_PROJECT_NAME": project,
                    "ALLOW_TCG_VM": "true",
                    "USE_KVM": "false",
                    "VM_IMAGE_REF": runtime_image,
                    **(
                        {
                            "ANDROID_ISO_HOST": str(media / "boot.iso"),
                            "ANDROID_IMAGE_REF": runtime_image,
                        }
                        if layout == "android"
                        else {}
                    ),
                },
            )
            execution = control.exec_run(
                [
                    "/app/.venv/bin/python",
                    "-c",
                    PROBE,
                    str(tmp_path),
                    runtime_image,
                    json.dumps([str(disks), str(tmp_path / "vm"), str(tmp_path / "phone-media")]),
                    "android" if layout == "android" else "qemu",
                ]
            )
            assert execution.exit_code == 0, execution.output.decode(errors="replace")
            proof = json.loads(execution.output.decode().splitlines()[-1])
            expected = disks / "overlays"
        proof["layout"] = layout
        (tmp_path / "host-paths-proof.json").write_text(json.dumps(proof, indent=2))
        assert "error_type" not in proof, proof
        assert proof["contents"] == contents
        template = (
            media / "boot.iso"
            if layout == "android"
            else tmp_path / "vm/desktop.qcow2"
            if layout == "local"
            else disks / "desktop.qcow2"
        )
        assert template.read_text() == contents
        copied = (
            media / "boot.iso" if layout == "android" else expected / proof["id"] / "disk.qcow2"
        )
        assert copied.read_text() == contents
        if layout == "android":
            actual = {m["Destination"]: m["Source"] for m in proof["mounts"]}
            assert actual == {
                "/android/boot.iso": str(media / "boot.iso"),
                "/android/kernel": str(media / "kernel"),
                "/android/initrd.img": str(media / "initrd.img"),
            }
        else:
            assert any(
                m["Source"] == str(copied.parent) and m["Destination"] == "/vm/instance"
                for m in proof["mounts"]
            )
        assert (
            not list((tmp_path / "vm/overlays").glob("*/disk.qcow2"))
            if layout != "local"
            else copied.is_file()
        )
    finally:
        for c in client.containers.list(all=True, filters={"label": f"{LABEL_PROJECT}={project}"}):
            with suppress(NotFound):
                c.remove(force=True)
        for n in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            n.remove()
        if volume is not None:
            volume.remove(force=True)
        if runtime_image is not None:
            image = client.images.get(runtime_image)
            assert image.attrs["Config"]["Labels"][LABEL_PROJECT] == project
            client.images.remove(runtime_image, force=True, noprune=True)
        client.close()
