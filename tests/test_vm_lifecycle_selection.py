"""Lifecycle checks use the inspected primary that will receive the command."""

import pytest
from httpx import ASGITransport
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.kvm import KvmUnavailableError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import (
    COMPOSE_SERVICE_LABEL,
    LABEL_DISK_COPY,
    LABEL_KIND,
    LABEL_VM_ID,
    LABEL_WS_FOR,
    VmPool,
)
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("USE_KVM", "false")
    monkeypatch.setenv("ALLOW_TCG_VM", "false")
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "state.json")
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())

    def add(kind="container", *, name="primary", role=None, vm_id="desk-selected"):
        labels = {LABEL_VM_ID: vm_id}
        if kind is not None:
            labels[LABEL_KIND] = kind
        if role is not None:
            labels[role] = vm_id
        container = _FakeContainer(name, status="exited", labels=labels)
        client.containers.items.append(container)
        return container

    return pool, client, add


@pytest.mark.parametrize("operation,flag", [("start_vm", "started"), ("restart_vm", "restarted")])
def test_a_desktop_is_not_reclassified_by_a_second_lookup_before_the_command(
    environment, operation, flag
):
    pool, client, add = environment
    primary = add()
    listing = client.containers.list

    def unstable_listing(*args, **kwargs):
        labels = (kwargs.get("filters") or {}).get("label", [])
        if f"{LABEL_VM_ID}=desk-selected" in labels and not getattr(primary, flag):
            if getattr(unstable_listing, "selected", False):
                raise ReadTimeout("discovery failed after the target was inspected")
            unstable_listing.selected = True
        return listing(*args, **kwargs)

    client.containers.list = unstable_listing
    result = getattr(pool, operation)("desk-selected")
    assert result.kind == "container" and result.status == "running"
    assert getattr(primary, flag)


@pytest.mark.parametrize("operation", ["start_vm", "restart_vm"])
@pytest.mark.parametrize("kind", ["qemu", "android", None])
def test_a_replacement_desktop_cannot_bypass_the_selected_guests_kvm_check(
    environment, operation, kind
):
    pool, client, add = environment
    selected = add(kind)
    replacement = _FakeContainer(
        "replacement",
        status="exited",
        labels={LABEL_VM_ID: "desk-selected", LABEL_KIND: "container"},
    )
    listing = client.containers.list
    seen = False

    def replaced_listing(*args, **kwargs):
        nonlocal seen
        labels = (kwargs.get("filters") or {}).get("label", [])
        if f"{LABEL_VM_ID}=desk-selected" in labels:
            if seen:
                return [replacement]
            seen = True
        return listing(*args, **kwargs)

    client.containers.list = replaced_listing
    with pytest.raises(KvmUnavailableError):
        getattr(pool, operation)("desk-selected")
    assert not any(
        (selected.started, selected.restarted, replacement.started, replacement.restarted)
    )


@pytest.mark.parametrize("operation", ["start_vm", "restart_vm"])
def test_ambiguous_dynamic_primaries_are_rejected_before_any_command(environment, operation):
    pool, _, add = environment
    primaries = [add(name="first"), add(name="second")]
    with pytest.raises(DockerUnavailableError, match="複数"):
        getattr(pool, operation)("desk-selected")
    assert not any(c.started or c.restarted for c in primaries)


@pytest.mark.parametrize("operation", ["start_vm", "restart_vm"])
def test_unknown_guest_kinds_are_not_allowed_to_bypass_kvm_checks(environment, operation):
    pool, _, add = environment
    primary = add(kind="unsupported")
    with pytest.raises(DockerUnavailableError, match="種別"):
        getattr(pool, operation)("desk-selected")
    assert not primary.started and not primary.restarted


@pytest.mark.parametrize("fixed", [False, True])
@pytest.mark.parametrize("role", [LABEL_WS_FOR, LABEL_DISK_COPY])
async def test_auxiliary_containers_without_a_primary_are_not_started(environment, fixed, role):
    _, _, add = environment
    vm_id = "desktop" if fixed else "desk-selected"
    auxiliary = add(role=role, vm_id=vm_id)
    if fixed:
        auxiliary.attrs["Config"]["Labels"][COMPOSE_SERVICE_LABEL] = "websockify-desktop"
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await api.post(f"/vms/{vm_id}/start")
    assert response.status_code == 404, response.text
    assert not auxiliary.started


@pytest.mark.parametrize("operation,flag", [("start_vm", "started"), ("stop_vm", "stopped")])
def test_preparation_jobs_are_never_replayed_as_environment_commands(environment, operation, flag):
    pool, _, add = environment
    copy = add(name="disk-copy", role=LABEL_DISK_COPY)
    relay = add(name="relay", role=LABEL_WS_FOR)
    primary = add()
    result = getattr(pool, operation)("desk-selected")
    assert result.id == "desk-selected"
    assert getattr(primary, flag) and getattr(relay, flag)
    assert not getattr(copy, flag)


@pytest.mark.parametrize("kind", ["container", "qemu", "android", None])
@pytest.mark.parametrize("operation", ["start_vm", "restart_vm"])
def test_known_kinds_keep_the_existing_explicit_tcg_opt_in(
    environment, monkeypatch, kind, operation
):
    pool, _, add = environment
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    primary = add(kind)
    result = getattr(pool, operation)("desk-selected")
    assert result.kind == (kind or "qemu") and result.status == "running"
    assert primary.started if operation == "start_vm" else primary.restarted
