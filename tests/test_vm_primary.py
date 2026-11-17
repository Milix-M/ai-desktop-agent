"""Keep display, isolation and Android setup on the same primary container."""

import pytest

from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def primary_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")

    def fixed(name, service, *, project="ai-desktop-agent"):
        container = _FakeContainer(
            name,
            labels={"com.docker.compose.service": service, "com.docker.compose.project": project},
        )
        client.containers.items.append(container)
        client.networks.get("ai-desktop-agent_default").connect(container)
        return container

    return pool, client, fixed


@pytest.mark.parametrize("service", ["vm", "desktop", "android"])
@pytest.mark.parametrize("separator", ["-", "_"])
def test_instance_identity_matches_the_display_with_a_manual_shadow(
    primary_environment, service, separator
):
    pool, _, fixed = primary_environment
    fixed("other-project", service, project="other")
    fixed("manual-shadow", service)
    primary = fixed(f"ai-desktop-agent{separator}{service}{separator}1", service)
    assert pool.get_vm(service).vnc_host == primary.name
    assert pool.instance_id(service) == primary.id


@pytest.mark.parametrize("service", ["vm", "desktop", "android"])
def test_preparation_only_isolates_the_selected_primary_but_deletion_still_cleans_the_group(
    primary_environment, service
):
    pool, client, fixed = primary_environment
    shadow = fixed("manual-shadow", service)
    primary = fixed(f"ai-desktop-agent-{service}-1", service)
    foreign = fixed("foreign-primary", service, project="other")
    pool.prepare_vm(service)
    private = client.networks.get(f"ai-desktop-agent_env_{service}")
    assert set(private.attrs["Containers"]) == {client.backend.id, primary.id}
    for unselected in (shadow, foreign):
        assert set(unselected.attrs["NetworkSettings"]["Networks"]) == {"ai-desktop-agent_default"}
    assert pool.remove_vm(service)
    assert primary.removed and shadow.removed
    assert not foreign.stopped and not foreign.removed


def test_fixed_manual_only_environment_keeps_its_existing_fallback(primary_environment):
    pool, _, fixed = primary_environment
    manual = fixed("manual-only", "android")
    assert pool.get_vm("android").vnc_host == manual.name
    assert pool.instance_id("android") == manual.id
    pool.prepare_vm("android")


@pytest.mark.parametrize("operation", ["list", "identity", "prepare"])
def test_duplicate_dynamic_mains_are_rejected_before_selecting_an_execution_target(
    primary_environment, operation
):
    pool, client, _ = primary_environment
    for name in ("first-main", "second-main"):
        client.containers.items.append(
            _FakeContainer(name, labels={"ai-desktop-agent.vm-id": "and-duplicate"})
        )
    with pytest.raises(DockerUnavailableError, match="本体が複数"):
        if operation == "list":
            pool.list_vms()
        elif operation == "identity":
            pool.instance_id("and-duplicate")
        else:
            pool.prepare_vm("and-duplicate")
    assert "ai-desktop-agent_env_and-duplicate" not in client.networks.items


def test_dynamic_identity_and_preparation_ignore_copy_helpers_and_relays(primary_environment):
    pool, client, _ = primary_environment
    for name, extra in (
        ("copy", {"ai-desktop-agent.disk-copy-for": "and-target"}),
        ("relay", {"ai-desktop-agent.ws-for": "and-target"}),
        ("primary", {}),
    ):
        client.containers.items.append(
            _FakeContainer(name, labels={"ai-desktop-agent.vm-id": "and-target", **extra})
        )
    assert pool.get_vm("and-target").vnc_host == "primary"
    assert pool.instance_id("and-target") == "primary"
    pool.prepare_vm("and-target")
    members = client.networks.get("ai-desktop-agent_env_and-target").attrs["Containers"]
    assert set(members) == {client.backend.id, "primary"}
