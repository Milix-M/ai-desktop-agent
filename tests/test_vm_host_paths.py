"""Bind sources follow the actual mounts, independently of task-data storage."""

import json
from pathlib import Path

import pytest
from docker.errors import NotFound
from httpx import ASGITransport
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeDocker


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    monkeypatch.setenv("USE_KVM", "false")
    monkeypatch.setenv("VM_IMAGE_REF", "test-vm-image:latest")
    monkeypatch.delenv("VM_HOST_DIR", raising=False)
    monkeypatch.delenv("ANDROID_ISO_HOST", raising=False)
    client = _FakeDocker()
    pool = VmPool(
        client=client, state_file=tmp_path / "state.json", overlays_dir=str(tmp_path / "view")
    )
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    return pool, client


def mount(source, destination, kind="bind"):
    return {"Type": kind, "Source": str(source), "Destination": destination}


def test_local_launch_passes_absolute_host_paths_to_docker(environment, tmp_path):
    pool, client = environment
    lookup = client.containers.get

    def local(name):
        if name == client.backend.name:
            raise NotFound("the backend runs directly on the host")
        return lookup(name)

    client.containers.get = local
    copied = pool._create_overlay("vm-local-copy", image="test-vm-image:latest")
    [copy] = client.containers.runs
    assert str(tmp_path / "vm/desktop.qcow2") in copy["volumes"]
    assert all(Path(p).is_absolute() for p in copy["volumes"])
    assert Path(copied) == tmp_path / "view/vm-local-copy/disk.qcow2"


@pytest.mark.parametrize("data_kind", ["bind", "volume"])
def test_vm_files_follow_the_vm_mount_instead_of_the_metadata_location(
    environment, tmp_path, data_kind
):
    pool, client = environment
    client.backend.attrs["Mounts"] = [
        mount(tmp_path / "metadata", "/app/data", data_kind),
        mount(tmp_path / "disks", "/vm"),
        mount(tmp_path / "disks/overlays", str(tmp_path / "view")),
    ]
    result = pool.create_vm(kind="qemu")
    copy, runtime = client.containers.runs
    assert str(tmp_path / "disks/desktop.qcow2") in copy["volumes"]
    assert str(tmp_path / "disks/overlays" / result.id) in copy["volumes"]
    assert str(tmp_path / "disks/sockets") in runtime["volumes"]


def test_individual_resource_mounts_and_the_longest_parent_are_respected(environment, tmp_path):
    pool, client = environment
    client.backend.attrs["Mounts"] = [
        mount(tmp_path / "metadata", "/app/data"),
        mount(tmp_path / "old-vm", "/vm"),
        mount(tmp_path / "base.qcow2", "/vm/desktop.qcow2"),
        mount(tmp_path / "disk-bank", str(tmp_path / "view")),
        mount(tmp_path / "endpoints", "/vm/sockets"),
    ]
    result = pool.create_vm(kind="qemu")
    copy, runtime = client.containers.runs
    assert str(tmp_path / "base.qcow2") in copy["volumes"]
    assert str(tmp_path / "disk-bank" / result.id) in copy["volumes"]
    assert str(tmp_path / "endpoints") in runtime["volumes"]
    assert not any("old-vm" in p for p in copy["volumes"])


@pytest.mark.parametrize("failure", ["lookup", "volume", "relative", "unmounted"])
async def test_unknown_host_sources_stop_before_a_copy_and_keep_cleanup_retryable(
    environment, tmp_path, failure
):
    pool, client = environment
    mounts = [
        mount(tmp_path / "repository/data", "/app/data"),
        mount(tmp_path / "vm", "/vm"),
        mount(tmp_path / "overlays", str(tmp_path / "view")),
    ]
    if failure == "lookup":
        lookup = client.containers.get
        image = pool._vm_image_ref
        selecting_files = False

        def selected(docker):
            nonlocal selecting_files
            result = image(docker)
            selecting_files = True
            return result

        def unavailable(name):
            if name == client.backend.name and selecting_files:
                raise ReadTimeout("backend inspection failed")
            return lookup(name)

        client.containers.get = unavailable
        pool._vm_image_ref = selected
    elif failure == "volume":
        mounts[-1]["Type"] = "volume"
    elif failure == "relative":
        mounts[-1]["Source"] = "relative-source"
    else:
        mounts.pop()
    client.backend.attrs["Mounts"] = mounts
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await api.post("/vms", json={"kind": "qemu"})
    assert response.status_code == 503, response.text
    assert client.containers.runs == []
    if failure == "lookup":
        [vm_id] = json.loads(pool._state_file.read_text())
        client.containers.get = lookup
        assert pool.remove_vm(vm_id)
    assert json.loads(pool._state_file.read_text()) == {}
    assert set(client.networks.items) == {"ai-desktop-agent_default"}


def test_android_files_follow_the_android_mount(environment, tmp_path, monkeypatch):
    pool, client = environment
    source = tmp_path / "phone-media"
    source.mkdir()
    for name in ["android-x86_64.iso", "kernel", "initrd.img"]:
        (source / name).write_bytes(b"android media")
    client.backend.attrs["Mounts"] = [
        mount(tmp_path / "separate-metadata", "/app/data"),
        mount(source, "/app/android-host"),
    ]
    result = pool.create_vm(kind="android")
    assert result.kind == "android"
    runtime = client.containers.runs[0]
    assert set(runtime["volumes"]) == {
        str(source / n) for n in ["android-x86_64.iso", "kernel", "initrd.img"]
    }


@pytest.mark.parametrize("source", ["relative/repository", " "])
def test_relative_explicit_repositories_are_rejected_before_disk_work(
    environment, monkeypatch, source
):
    pool, client = environment
    monkeypatch.setenv("VM_HOST_DIR", source)
    with pytest.raises(DockerUnavailableError):
        pool.create_vm(kind="qemu")
    assert client.containers.runs == []


@pytest.mark.parametrize(
    "destination,suffix",
    [
        ("/vm", "vm"),
        ("/vm/overlays", "vm/overlays"),
        ("/vm/sockets", "vm/sockets"),
        ("/app/android-host", "android"),
        ("/app/data", "data"),
        ("/app", ""),
    ],
)
def test_legacy_repository_inference_requires_a_matching_standard_bind_layout(
    environment, tmp_path, destination, suffix
):
    pool, client = environment
    root = tmp_path / "repository"
    client.backend.attrs["Mounts"] = [mount(root / suffix, destination)]
    assert pool._host_repo_dir() == str(root)


@pytest.mark.parametrize(
    "mounts",
    [
        [],
        [mount("/srv/custom-metadata", "/app/data")],
        [mount("/var/lib/docker/volumes/metadata/_data", "/app/data", "volume")],
        [mount("relative/data", "/app/data")],
    ],
)
def test_a_repository_is_not_guessed_from_an_unrelated_or_non_bind_source(environment, mounts):
    pool, client = environment
    client.backend.attrs["Mounts"] = mounts
    with pytest.raises(DockerUnavailableError):
        pool._host_repo_dir()


def test_default_local_disk_storage_and_cleanup_share_the_same_directory(environment, tmp_path):
    _, client = environment
    lookup = client.containers.get

    def local(name):
        if name == client.backend.name:
            raise NotFound("host process")
        return lookup(name)

    client.containers.get = local
    pool = VmPool(client=client, state_file=tmp_path / "local-state.json")
    copied = Path(pool._create_overlay("vm-local-copy", image="test-vm-image:latest"))
    assert copied == tmp_path / "vm/overlays/vm-local-copy/disk.qcow2"
    copied.write_bytes(b"the local disk")
    neighbor = tmp_path / "vm/overlays/vm-other"
    neighbor.mkdir()
    (neighbor / "disk.qcow2").write_bytes(b"keep another instance")
    pool._remove_disk("vm-local-copy")
    assert not copied.parent.exists()
    assert (neighbor / "disk.qcow2").read_bytes() == b"keep another instance"


def test_a_standard_data_bind_can_locate_an_unmounted_read_only_template(environment, tmp_path):
    pool, client = environment
    root = tmp_path / "repository"
    client.backend.attrs["Mounts"] = [
        mount(root / "data", "/app/data"),
        mount(tmp_path / "disk-bank", str(tmp_path / "view")),
        mount(tmp_path / "endpoints", "/vm/sockets"),
    ]
    pool.create_vm(kind="qemu")
    assert str(root / "vm/desktop.qcow2") in client.containers.runs[0]["volumes"]


def test_an_explicit_host_iso_can_be_verified_through_its_actual_backend_mount(
    environment, tmp_path, monkeypatch
):
    pool, client = environment
    source = Path("/daemon/media")
    monkeypatch.setenv("ANDROID_ISO_HOST", str(source / "custom.iso"))
    client.backend.attrs["Mounts"] = [mount(source, "/app/android-host")]
    is_file = Path.is_file

    def visible(path):
        if str(path) == "/app/android-host/custom.iso":
            return True
        return False if path.is_relative_to(source) else is_file(path)

    monkeypatch.setattr(Path, "is_file", visible)
    result = pool.create_vm(kind="android")
    assert result.kind == "android"
    assert str(source / "custom.iso") in client.containers.runs[0]["volumes"]


def test_an_automatic_iso_can_be_verified_through_the_backend_view(environment, monkeypatch):
    pool, client = environment
    client.backend.attrs["Mounts"] = [mount("/daemon/media", "/app/android-host")]
    monkeypatch.setattr(
        Path, "is_file", lambda path: str(path) == "/app/android-host/android-x86_64.iso"
    )
    result = pool.create_vm(kind="android")
    assert result.kind == "android"
    assert "/daemon/media/android-x86_64.iso" in client.containers.runs[0]["volumes"]


@pytest.mark.parametrize(
    "source", ["/elsewhere/custom.iso", "/daemon/media/missing.iso", "relative.iso"]
)
def test_an_unverified_or_relative_explicit_iso_is_rejected(environment, monkeypatch, source):
    pool, client = environment
    client.backend.attrs["Mounts"] = [mount("/daemon/media", "/app/android-host")]
    monkeypatch.setenv("ANDROID_ISO_HOST", source)
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) == "/app/android-host/custom.iso")
    error = DockerUnavailableError if source == "relative.iso" else ValueError
    with pytest.raises(error):
        pool.create_vm(kind="android")
    assert client.containers.runs == []


async def test_missing_optional_android_media_remains_a_user_configuration_error(environment):
    pool, client = environment
    client.backend.attrs["Mounts"] = []
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await api.post("/vms", json={"kind": "android"})
    assert response.status_code == 400, response.text
    assert "Android ISO" in response.json()["detail"]
    assert client.containers.runs == []
    assert json.loads(pool._state_file.read_text()) == {}


def test_an_iso_in_the_backend_without_a_host_mount_cannot_be_guessed(environment, monkeypatch):
    pool, client = environment
    client.backend.attrs["Mounts"] = []
    monkeypatch.setattr(
        Path, "is_file", lambda path: str(path) == "/app/android-host/android-x86_64.iso"
    )
    with pytest.raises(DockerUnavailableError):
        pool.create_vm(kind="android")
    assert client.containers.runs == []


def test_explicit_available_iso_does_not_require_optional_kernel_mounts(
    environment, tmp_path, monkeypatch
):
    pool, client = environment
    iso = tmp_path / "boot.iso"
    iso.write_bytes(b"an explicitly available ISO")
    monkeypatch.setenv("ANDROID_ISO_HOST", str(iso))
    client.backend.attrs["Mounts"] = []
    assert pool.create_vm(kind="android").kind == "android"
    assert client.containers.runs[0]["volumes"] == {
        str(iso): {"bind": "/android/boot.iso", "mode": "ro"}
    }
