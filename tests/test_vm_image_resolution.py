"""New environments use the visible primary image and stop on unknown source state."""

import json
from types import SimpleNamespace

import httpx
import pytest
from docker.errors import APIError
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.networks import LABEL_VM_ID
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import COMPOSE_SERVICE_LABEL, VmPool
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker

SOURCE = "sha256:" + "a" * 64
REPLACEMENT = "sha256:" + "b" * 64
PROFILES = {
    "container": ("desktop", "DESKTOP_IMAGE_REF", "ai-desktop-agent-desktop:latest"),
    "qemu": ("vm", "VM_IMAGE_REF", "ai-desktop-agent-vm:latest"),
    "android": ("android", "ANDROID_IMAGE_REF", "ai-desktop-agent-android:latest"),
}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    monkeypatch.setenv("USE_KVM", "false")
    for _, variable, _ in PROFILES.values():
        monkeypatch.delenv(variable, raising=False)
    directory = tmp_path / "android"
    directory.mkdir()
    (directory / "android-x86_64.iso").write_bytes(b"fixture ISO")
    client = _FakeDocker()
    pool = VmPool(
        client=client,
        state_file=tmp_path / "vms.json",
        overlays_dir=str(tmp_path / "overlays"),
    )

    def prepare(kind, *, with_source=True):
        service, _, _ = PROFILES[kind]
        primary = _FakeContainer(
            f"ai-desktop-agent-{service}-1", labels={COMPOSE_SERVICE_LABEL: service}
        )
        primary.attrs["Image"] = SOURCE
        primary.image = SimpleNamespace(attrs={"RepoTags": ["shared-source:latest"]})
        if with_source:
            client.containers.items.append(primary)
        return pool, client, primary

    return prepare


@pytest.mark.parametrize("kind", PROFILES)
def test_creation_pins_the_primary_image_id_instead_of_a_mutable_tag(environment, kind):
    pool, client, _ = environment(kind)
    result = pool.create_vm(kind=kind)
    assert result.kind == kind
    assert client.containers.runs and all(r["image"] == SOURCE for r in client.containers.runs)


@pytest.mark.parametrize("kind", PROFILES)
def test_creation_uses_the_same_primary_as_the_visible_environment_despite_list_order(
    environment, kind
):
    pool, client, primary = environment(kind)
    service, _, _ = PROFILES[kind]
    shadow = _FakeContainer(f"manual-{service}-shadow", labels={COMPOSE_SERVICE_LABEL: service})
    shadow.attrs["Image"] = REPLACEMENT
    shadow.image = SimpleNamespace(attrs={"RepoTags": ["shadow-source:latest"]})
    client.containers.items.insert(0, shadow)
    assert pool.get_vm(service).vnc_host == primary.name
    pool.create_vm(kind=kind)
    assert all(r["image"] == SOURCE for r in client.containers.runs)


@pytest.mark.parametrize("kind", PROFILES)
async def test_source_lookup_failure_returns_503_and_reclaims_the_partial_allocation(
    environment, tmp_path, monkeypatch, kind
):
    pool, client, _ = environment(kind)
    service, _, _ = PROFILES[kind]
    allocated = False
    create, listing = client.networks.create, client.containers.list

    def network(name, **kwargs):
        nonlocal allocated
        result = create(name, **kwargs)
        if kwargs.get("labels", {}).get(LABEL_VM_ID):
            allocated = True
        return result

    def fail_source(*args, **kwargs):
        labels = (kwargs.get("filters") or {}).get("label", [])
        if allocated and f"{COMPOSE_SERVICE_LABEL}={service}" in labels:
            raise ReadTimeout("source discovery response lost")
        return listing(*args, **kwargs)

    client.networks.create = network
    client.containers.list = fail_source
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post("/vms", json={"kind": kind, "name": "new environment"})
    assert response.status_code == 503
    assert allocated and client.containers.runs == []
    assert set(client.networks.items) == {"ai-desktop-agent_default"}
    assert client.volumes.items == {}
    assert json.loads((tmp_path / "vms.json").read_text()) == {}
    assert not list((tmp_path / "overlays").glob("*/disk.qcow2"))


@pytest.mark.parametrize("kind", PROFILES)
@pytest.mark.parametrize("image", [None, "", []])
def test_missing_or_invalid_source_image_never_becomes_a_default_image_launch(
    environment, tmp_path, kind, image
):
    pool, client, primary = environment(kind)
    primary.attrs["Image"] = image
    with pytest.raises(DockerUnavailableError, match="イメージ"):
        pool.create_vm(kind=kind)
    assert client.containers.runs == [] and client.volumes.items == {}
    assert set(client.networks.items) == {"ai-desktop-agent_default"}
    assert json.loads((tmp_path / "vms.json").read_text()) == {}


@pytest.mark.parametrize("kind", PROFILES)
def test_an_explicit_image_override_takes_precedence_over_the_detected_source(
    environment, monkeypatch, kind
):
    pool, client, primary = environment(kind)
    _, variable, _ = PROFILES[kind]
    primary.attrs["Image"] = None
    monkeypatch.setenv(variable, "explicit-source:reviewed")
    pool.create_vm(kind=kind)
    assert all(r["image"] == "explicit-source:reviewed" for r in client.containers.runs)


@pytest.mark.parametrize("kind", PROFILES)
def test_a_genuinely_absent_fixed_environment_uses_the_documented_default(environment, kind):
    pool, client, _ = environment(kind, with_source=False)
    _, _, default = PROFILES[kind]
    pool.create_vm(kind=kind)
    assert all(r["image"] == default for r in client.containers.runs)


def test_qemu_copy_and_runtime_keep_one_selected_image_when_the_base_is_replaced(environment):
    pool, client, primary = environment("qemu")
    run = client.containers.run

    def replace_after_copy(image, **kwargs):
        result = run(image, **kwargs)
        if kwargs.get("remove"):
            replacement = _FakeContainer(primary.name, labels={COMPOSE_SERVICE_LABEL: "vm"})
            replacement.id = "replacement-instance"
            replacement.attrs["Image"] = REPLACEMENT
            replacement.image = SimpleNamespace(attrs={"RepoTags": ["replacement:latest"]})
            client.containers.items = [
                replacement if container is primary else container
                for container in client.containers.items
            ]
        return result

    client.containers.run = replace_after_copy
    pool.create_vm(kind="qemu")
    assert len(client.containers.runs) == 2
    assert [call["image"] for call in client.containers.runs] == [SOURCE, SOURCE]


@pytest.mark.parametrize("kind", PROFILES)
@pytest.mark.parametrize("fault", ["api", "response-lost"])
async def test_a_failed_launch_returns_503_and_removes_an_accepted_container(
    environment, tmp_path, monkeypatch, kind, fault
):
    pool, client, primary = environment(kind)
    run, accepted = client.containers.run, []

    def uncertain_launch(image, **kwargs):
        result = run(image, **kwargs)
        if kwargs.get("detach"):
            accepted.append(result)
            if fault == "api":
                raise APIError("daemon rejected launch acknowledgement")
            raise ReadTimeout("launch response lost")
        return result

    client.containers.run = uncertain_launch
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as api:
        response = await api.post("/vms", json={"kind": kind})
    assert response.status_code == 503
    assert len(accepted) == 1 and accepted[0].removed
    assert not primary.removed and not primary.stopped
    assert set(client.networks.items) == {"ai-desktop-agent_default"}
    assert client.volumes.items == {}
    assert json.loads((tmp_path / "vms.json").read_text()) == {}


async def test_failed_rollback_preserves_ownership_so_the_partial_environment_can_be_deleted(
    environment, tmp_path, monkeypatch
):
    pool, client, primary = environment("container")
    run, accepted, removers = client.containers.run, [], []

    def uncertain_launch(image, **kwargs):
        result = run(image, **kwargs)
        accepted.append(result)
        removers.append(result.remove)

        def cannot_remove(*args, **kwargs):
            raise APIError("temporary removal failure")

        result.remove = cannot_remove
        raise ReadTimeout("launch response lost")

    client.containers.run = uncertain_launch
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as api:
        response = await api.post("/vms", json={"kind": "container"})
    assert response.status_code == 503
    partial = accepted[0]
    identifier = partial.attrs["Config"]["Labels"][LABEL_VM_ID]
    saved = json.loads((tmp_path / "vms.json").read_text())[identifier]
    assert saved["isolated_network"] in client.networks.items
    assert saved["home_volume"] in client.volumes.items
    partial.remove = removers[0]
    assert pool.remove_vm(identifier) is True
    assert json.loads((tmp_path / "vms.json").read_text()) == {}
    assert client.volumes.items == {}
    assert not primary.removed and not primary.stopped


async def test_disk_directory_failure_returns_503_and_preserves_data_for_repair(
    environment, tmp_path, monkeypatch
):
    pool, client, primary = environment("qemu")
    blocker = tmp_path / "overlays"
    blocker.write_text("pre-existing data")
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as api:
        response = await api.post("/vms", json={"kind": "qemu"})
    assert response.status_code == 503
    assert blocker.read_text() == "pre-existing data" and client.containers.runs == []
    records = json.loads((tmp_path / "vms.json").read_text())
    assert len(records) == 1
    identifier = next(iter(records))
    blocker.unlink()
    blocker.mkdir()
    assert pool.remove_vm(identifier) is True
    assert json.loads((tmp_path / "vms.json").read_text()) == {}
    assert not primary.removed and not primary.stopped
