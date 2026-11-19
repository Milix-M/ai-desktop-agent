"""Malformed external subnet inspection must not expose a management listener."""

from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.networks import EnvironmentNetworks
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from tests.test_vm_pool import _FakeDocker


@pytest.mark.parametrize(
    "configs", [[{}], [{"Subnet": "192.0.2.0/24"}]], ids=["missing", "outside"]
)
def test_management_subnets_reject_incomplete_or_nonmatching_daemon_inspection(
    monkeypatch, configs
):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "network-subnets")
    client = _FakeDocker()
    network = client.networks.get("network-subnets_default")
    # This is an externally supplied inspection payload, not application state.
    network.attrs["IPAM"] = {"Config": configs}
    network.connect = Mock(wraps=network.connect)
    network.disconnect = Mock(wraps=network.disconnect)
    network.remove = Mock(wraps=network.remove)
    manager = EnvironmentNetworks(client, "network-subnets")
    with pytest.raises(DockerUnavailableError, match="サブネット"):
        manager.management_subnets()
    network.connect.assert_not_called()
    network.disconnect.assert_not_called()
    network.remove.assert_not_called()
    assert set(network.attrs["Containers"]) == {client.backend.id}
    network.attrs["IPAM"] = {"Config": [{"Subnet": "172.30.0.0/16"}]}
    assert manager.management_subnets() == ["172.30.0.0/16"]


@pytest.mark.parametrize("operation", ["ensure", "remove"])
def test_invalid_network_id_is_rejected_before_allocation_or_endpoint_changes(
    monkeypatch, operation
):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "network-ids")
    client = _FakeDocker()
    manager = EnvironmentNetworks(client, "network-ids")
    network = client.networks.get("network-ids_default")
    before = set(client.networks.items)
    client.networks.create = Mock(wraps=client.networks.create)
    network.connect = Mock(wraps=network.connect)
    network.disconnect = Mock(wraps=network.disconnect)
    network.remove = Mock(wraps=network.remove)
    with pytest.raises(ValueError, match="環境ID"):
        getattr(manager, operation)("../foreign")
    client.networks.create.assert_not_called()
    network.connect.assert_not_called()
    network.disconnect.assert_not_called()
    network.remove.assert_not_called()
    assert set(client.networks.items) == before
    assert set(network.attrs["Containers"]) == {client.backend.id}
    assert manager.name_for("desktop") == "network-ids_env_desktop"
