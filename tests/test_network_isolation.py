import json
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.server import __main__ as entrypoint
from ai_desktop_agent.server.browser_access import ManagementNetworkMiddleware
from ai_desktop_agent.server.networks import LABEL_ISOLATED, EnvironmentNetworks
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, LABEL_VM_ID, VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


def pool_for(tmp_path):
    docker = _FakeDocker()
    return VmPool(
        client=docker, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path / "disks")
    ), docker


def test_desktops_use_separate_bridges_and_backend_keeps_its_management_interface(tmp_path):
    pool, docker = pool_for(tmp_path)
    first = pool.create_vm(kind="container")
    second = pool.create_vm(kind="container")
    a, b = (docker.containers.get(v.vnc_host) for v in [first, second])
    a_nets = set(a.attrs["NetworkSettings"]["Networks"])
    b_nets = set(b.attrs["NetworkSettings"]["Networks"])
    assert len(a_nets) == len(b_nets) == 1 and a_nets.isdisjoint(b_nets)
    assert a_nets | b_nets <= set(docker.backend.attrs["NetworkSettings"]["Networks"])
    assert EnvironmentNetworks(docker, "ai-desktop-agent").management_address() == "172.30.0.2"
    assert all(run.get("cap_drop") == ["NET_RAW"] for run in docker.containers.runs)
    pool.remove_vm(first.id)
    assert not a_nets & set(docker.networks.items)
    assert b_nets <= set(docker.networks.items) and not b.removed


def test_legacy_migration_keeps_the_container_and_remains_discoverable_after_reconnect(tmp_path):
    pool, docker = pool_for(tmp_path)
    old = docker.networks.get("ai-desktop-agent_default")
    guest = _FakeContainer("legacy-desktop", labels={LABEL_VM_ID: "desk-old"})
    guest.attrs["Config"]["Labels"].pop(LABEL_PROJECT)
    docker.containers.items.append(guest)
    old.connect(guest)
    pool._save_state("desk-old", {"name": "保存した作業環境"})
    pool.prepare_vm("desk-old")
    private = next(iter(guest.attrs["NetworkSettings"]["Networks"]))
    assert private != "ai-desktop-agent_default"
    assert not guest.removed and pool.get_vm("desk-old").name == "保存した作業環境"
    docker.networks.get(private).disconnect(docker.backend)
    assert pool.get_vm("desk-old").vnc_host == guest.name
    pool.isolate_environments()
    assert private in docker.backend.attrs["NetworkSettings"]["Networks"]


@pytest.mark.parametrize("mismatch", ["project", "vm-id", "driver", "member"])
def test_colliding_or_shared_networks_are_not_adopted_or_modified(tmp_path, mismatch):
    pool, docker = pool_for(tmp_path)
    manager = EnvironmentNetworks(docker, "ai-desktop-agent")
    labels = {
        LABEL_PROJECT: "ai-desktop-agent",
        LABEL_VM_ID: "desk-protected",
        LABEL_ISOLATED: "true",
    }
    network = docker.networks.create(manager.name_for("desk-protected"), labels=labels)
    outsider = _FakeContainer("other-project", labels={LABEL_PROJECT: "other"})
    if mismatch == "project":
        labels[LABEL_PROJECT] = "other"
    elif mismatch == "vm-id":
        labels[LABEL_VM_ID] = "desk-other"
    elif mismatch == "driver":
        network.attrs["Driver"] = "host"
    else:
        network.connect(outsider)
    before = json.dumps(network.attrs, sort_keys=True)
    with pytest.raises(DockerUnavailableError):
        manager.ensure("desk-protected")
    assert json.dumps(network.attrs, sort_keys=True) == before
    assert network.name in docker.networks.items and not outsider.removed


def test_failed_detachment_preserves_discovery_and_can_be_retried(tmp_path, monkeypatch):
    pool, docker = pool_for(tmp_path)
    guest = _FakeContainer("legacy", labels={LABEL_VM_ID: "desk-old"})
    guest.attrs["Config"]["Labels"].pop(LABEL_PROJECT)
    docker.containers.items.append(guest)
    old = docker.networks.get("ai-desktop-agent_default")
    old.connect(guest)
    pool._save_state("desk-old", {"name": "保存した環境"})
    original = old.disconnect
    monkeypatch.setattr(old, "disconnect", Mock(side_effect=RuntimeError("Docker disconnected")))
    with pytest.raises(DockerUnavailableError):
        pool.prepare_vm("desk-old")
    assert not guest.removed and len(guest.attrs["NetworkSettings"]["Networks"]) == 2
    assert pool.get_vm("desk-old") is not None
    monkeypatch.setattr(old, "disconnect", original)
    pool.prepare_vm("desk-old")
    assert len(guest.attrs["NetworkSettings"]["Networks"]) == 1
    assert pool.get_vm("desk-old").name == "保存した環境"


@pytest.mark.parametrize("kind", ["fixed", "dynamic"])
@pytest.mark.parametrize("operation", ["list", "migrate", "prepare", "start"])
def test_discovery_failure_cannot_be_mistaken_for_a_missing_environment(
    tmp_path, monkeypatch, kind, operation
):
    pool, docker = pool_for(tmp_path)
    original = docker.containers.list

    def unavailable(*args, **kwargs):
        label = kwargs["filters"]["label"]
        if label.startswith("com.docker.compose.service" if kind == "fixed" else LABEL_VM_ID):
            raise RuntimeError("Docker disconnected")
        return original(*args, **kwargs)

    monkeypatch.setattr(docker.containers, "list", unavailable)
    vm_id = "desktop" if kind == "fixed" else "desk-saved"
    with pytest.raises(DockerUnavailableError, match="作業環境を確認できません"):
        if operation == "list":
            pool.list_vms()
        elif operation == "migrate":
            pool.isolate_environments()
        elif operation == "prepare":
            pool.prepare_vm(vm_id)
        else:
            pool.start_vm(vm_id)
    assert not docker.containers.runs


@pytest.mark.parametrize("vm_id", [None, "desktop"])
@pytest.mark.parametrize("stage", ["discovery", "isolation"])
async def test_unverified_environment_returns_503_without_connecting_or_starting_a_task(
    tmp_path, monkeypatch, vm_id, stage
):
    from httpx import ASGITransport

    from ai_desktop_agent.server import app as server
    from ai_desktop_agent.server.store import TaskStore
    from tests.http import AuthenticatedClient

    pool, docker = pool_for(tmp_path)
    guest = _FakeContainer("desktop", labels={"com.docker.compose.service": "desktop"})
    docker.containers.items.append(guest)
    docker.networks.get("ai-desktop-agent_default").connect(guest)
    if stage == "discovery":
        monkeypatch.setattr(docker.containers, "list", Mock(side_effect=RuntimeError("offline")))
    else:
        monkeypatch.setattr(
            EnvironmentNetworks, "isolate", Mock(side_effect=DockerUnavailableError("offline"))
        )
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_store", TaskStore(root=tmp_path / "tasks"))
    connect = Mock()
    monkeypatch.setattr(server, "_connect_vm_display", connect)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        assert (await api.get("/vms")).status_code == 503
        response = await api.post("/tasks", json={"instruction": "作業を開始", "vm_id": vm_id})
        assert response.status_code == 503
    connect.assert_not_called()
    assert server.get_store().list() == []


def test_failed_container_creation_releases_the_new_bridge(tmp_path, monkeypatch):
    pool, docker = pool_for(tmp_path)
    monkeypatch.setattr(
        docker.containers, "run", Mock(side_effect=RuntimeError("image unavailable"))
    )
    with pytest.raises(RuntimeError):
        pool.create_vm(kind="container")
    assert set(docker.networks.items) == {"ai-desktop-agent_default"}
    assert pool._load_all_states() == {}


def test_failed_creation_cleanup_keeps_metadata_until_bridge_removal_succeeds(
    tmp_path, monkeypatch
):
    pool, docker = pool_for(tmp_path)
    monkeypatch.setattr(
        docker.containers, "run", Mock(side_effect=RuntimeError("image unavailable"))
    )
    original = EnvironmentNetworks.remove
    monkeypatch.setattr(
        EnvironmentNetworks, "remove", Mock(side_effect=DockerUnavailableError("offline"))
    )
    with pytest.raises(RuntimeError, match="image unavailable"):
        pool.create_vm(kind="container")
    states = pool._load_all_states()
    assert len(states) == 1
    vm_id = next(iter(states))
    monkeypatch.setattr(EnvironmentNetworks, "remove", original)
    assert pool.remove_vm(vm_id)
    assert set(docker.networks.items) == {"ai-desktop-agent_default"}
    assert pool._load_all_states() == {}


def test_failed_network_deletion_keeps_metadata_so_cleanup_can_be_retried(tmp_path, monkeypatch):
    pool, docker = pool_for(tmp_path)
    created = pool.create_vm(kind="container")
    name = EnvironmentNetworks(docker, "ai-desktop-agent").name_for(created.id)
    network = docker.networks.get(name)
    original = network.remove
    monkeypatch.setattr(network, "remove", Mock(side_effect=RuntimeError("Docker unavailable")))
    with pytest.raises(DockerUnavailableError):
        pool.remove_vm(created.id)
    assert pool._load_state(created.id) is not None
    monkeypatch.setattr(network, "remove", original)
    assert pool.remove_vm(created.id)
    assert name not in docker.networks.items and pool._load_state(created.id) is None


@pytest.mark.parametrize("value", ["0.0.0.0", "::", "127.0.0.1", "invalid"])
def test_container_listener_never_falls_back_to_wildcard_or_loopback(value):
    docker = _FakeDocker()
    docker.backend.attrs["NetworkSettings"]["Networks"]["ai-desktop-agent_default"]["IPAddress"] = (
        value
    )
    with pytest.raises(DockerUnavailableError):
        EnvironmentNetworks(docker, "ai-desktop-agent").management_address()


@pytest.mark.parametrize("module_entry", [False, True])
def test_container_entrypoint_binds_only_management_ip_and_disables_forwarded_headers(
    tmp_path, monkeypatch, module_entry
):
    pool, docker = pool_for(tmp_path)
    monkeypatch.setenv("AGENT_MANAGEMENT_CIDRS", "127.0.0.0/8,::1/128")
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    monkeypatch.setattr(entrypoint, "VmPool", lambda: pool)
    run = Mock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    if module_entry:
        import runpy

        monkeypatch.setattr("ai_desktop_agent.server.vm_pool.VmPool", lambda: pool)
        runpy.run_path(entrypoint.__file__, run_name="__main__")
    else:
        entrypoint.main()
    assert run.call_args.kwargs["host"] == "172.30.0.2"
    assert run.call_args.kwargs["proxy_headers"] is False
    assert run.call_args.kwargs["workers"] == 1
    assert run.call_args.kwargs["ws"] is entrypoint.AgentWebSocketProtocol
    assert run.call_args.kwargs["ws_max_size"] == 1024 * 1024
    assert run.call_args.kwargs["ws_max_queue"] == 4
    import os

    assert os.environ["AGENT_MANAGEMENT_CIDRS"] == "172.30.0.0/16"


def test_management_gateway_priority_is_required():
    docker = _FakeDocker()
    docker.backend.attrs["NetworkSettings"]["Networks"]["ai-desktop-agent_default"][
        "GwPriority"
    ] = 0
    with pytest.raises(DockerUnavailableError, match="gw_priority"):
        EnvironmentNetworks(docker, "ai-desktop-agent").management_address()


@pytest.mark.parametrize("kind", ["http", "websocket"])
async def test_desktop_peer_cannot_use_management_api_even_with_forged_forwarded_headers(
    monkeypatch, kind
):
    monkeypatch.setenv("AGENT_MANAGEMENT_CIDRS", "172.30.0.0/16")
    target, send = AsyncMock(), AsyncMock()
    await ManagementNetworkMiddleware(target)(
        {
            "type": kind,
            "client": ("172.31.0.4", 4300),
            "headers": [(b"x-forwarded-for", b"172.30.0.1")],
        },
        AsyncMock(),
        send,
    )
    target.assert_not_awaited()
    message = send.call_args_list[0].args[0]
    assert message.get("status") == 403 if kind == "http" else message.get("code") == 1008


async def test_management_network_and_local_development_peers_are_allowed(monkeypatch):
    for networks, peer in [("172.30.0.0/16", "172.30.0.1"), ("127.0.0.0/8,::1/128", "::1")]:
        monkeypatch.setenv("AGENT_MANAGEMENT_CIDRS", networks)
        target = AsyncMock()
        await ManagementNetworkMiddleware(target)(
            {"type": "http", "client": (peer, 1)}, AsyncMock(), AsyncMock()
        )
        target.assert_awaited_once()
