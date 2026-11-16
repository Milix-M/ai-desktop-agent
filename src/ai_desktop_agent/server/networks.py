"""One bridge per environment, with the management API bound to a separate interface."""

import ipaddress
import os
import re
import socket
from functools import wraps

from docker.errors import NotFound

from ai_desktop_agent.server.vm_control import DockerUnavailableError

LABEL_PROJECT = "ai-desktop-agent.project"
LABEL_VM_ID = "ai-desktop-agent.vm-id"
LABEL_ISOLATED = "ai-desktop-agent.isolated"


def network_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (DockerUnavailableError, ValueError):
            raise
        except Exception as error:
            raise DockerUnavailableError(
                "環境のネットワーク操作に失敗しました。Dockerの状態と権限を確認してください"
            ) from error

    return guarded


class EnvironmentNetworks:
    def __init__(self, client, project: str):
        self.client = client
        self.project = project

    def backend(self):
        try:
            backend = self.client.containers.get(socket.gethostname())
            backend.reload()
            return backend
        except Exception as error:
            raise DockerUnavailableError(
                "バックエンドの管理ネットワークを確認できません"
            ) from error

    @property
    def management_name(self) -> str:
        return os.environ.get("AGENT_MANAGEMENT_NETWORK", f"{self.project}_default")

    def management_address(self) -> str:
        endpoints = self.backend().attrs.get("NetworkSettings", {}).get("Networks", {})
        endpoint = endpoints.get(self.management_name, {})
        if endpoint.get("GwPriority", 0) < 1:
            raise DockerUnavailableError(
                "管理ネットワークのgw_priorityを1以上にしてください。"
                "Docker API 1.48以降・Compose 2.33.1以降が必要です"
            )
        address = endpoint.get("IPAddress") or endpoint.get("GlobalIPv6Address", "")
        try:
            ip = ipaddress.ip_address(address)
            if ip.is_unspecified or ip.is_loopback:
                raise ValueError("not a management interface")
        except ValueError as error:
            raise DockerUnavailableError("管理ネットワークのIPを確認できません") from error
        return str(ip)

    @network_operation
    def management_subnets(self) -> list[str]:
        network = self.client.networks.get(self.management_name)
        subnets = [
            str(ipaddress.ip_network(config["Subnet"]))
            for config in (network.attrs.get("IPAM") or {}).get("Config", [])
            if config.get("Subnet")
        ]
        address = ipaddress.ip_address(self.management_address())
        if not any(address in ipaddress.ip_network(subnet) for subnet in subnets):
            raise DockerUnavailableError("管理ネットワークのサブネットを確認できません")
        return subnets

    def name_for(self, vm_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", vm_id):
            raise ValueError("環境IDが不正です")
        return f"{self.project}_env_{vm_id}"

    def owned(self, network, vm_id: str) -> bool:
        labels = network.attrs.get("Labels") or {}
        owner = labels.get(LABEL_PROJECT) or labels.get("com.docker.compose.project")
        return (
            owner == self.project
            and labels.get(LABEL_VM_ID) == vm_id
            and labels.get(LABEL_ISOLATED) == "true"
            and network.attrs.get("Driver") == "bridge"
        )

    @network_operation
    def ensure(self, vm_id: str, target=None):
        """Return a private bridge shared only by this backend and the selected environment."""
        self.management_address()
        name = self.name_for(vm_id)
        try:
            network = self.client.networks.get(name)
        except NotFound:
            network = self.client.networks.create(
                name,
                driver="bridge",
                labels={
                    LABEL_PROJECT: self.project,
                    LABEL_VM_ID: vm_id,
                    LABEL_ISOLATED: "true",
                },
            )
        network.reload()
        if not self.owned(network, vm_id):
            raise DockerUnavailableError(f"専用ネットワークの所属が一致しません: {name}")
        backend = self.backend()
        allowed = {backend.id}
        if target is not None:
            allowed.add(target.id)
        if set(network.attrs.get("Containers") or {}) - allowed:
            raise DockerUnavailableError(f"専用ネットワークに別のコンテナが接続しています: {name}")
        if name not in backend.attrs.get("NetworkSettings", {}).get("Networks", {}):
            network.connect(backend)
        return network

    @network_operation
    def isolate(self, vm_id: str, target) -> str:
        network = self.ensure(vm_id, target)
        target.reload()
        current = set(target.attrs.get("NetworkSettings", {}).get("Networks", {}))
        if network.name not in current:
            network.connect(target)
        # Join and verify the new bridge before detaching any existing connection.
        target.reload()
        if network.name not in target.attrs.get("NetworkSettings", {}).get("Networks", {}):
            raise DockerUnavailableError(f"専用ネットワークへ接続できません: {vm_id}")
        for name in current:
            if name != network.name:
                self.client.networks.get(name).disconnect(target)
        target.reload()
        if set(target.attrs.get("NetworkSettings", {}).get("Networks", {})) != {network.name}:
            raise DockerUnavailableError(f"環境のネットワークを分離できません: {vm_id}")
        return network.name

    @network_operation
    def remove(self, vm_id: str) -> None:
        """Delete only this environment's empty bridge; keep unknown members untouched."""
        try:
            network = self.client.networks.get(self.name_for(vm_id))
        except NotFound:
            return
        network.reload()
        if not self.owned(network, vm_id):
            raise DockerUnavailableError("削除対象ネットワークの所属が一致しません")
        backend = self.backend()
        if set(network.attrs.get("Containers") or {}) - {backend.id}:
            raise DockerUnavailableError("使用中の専用ネットワークは削除できません")
        if network.name in backend.attrs.get("NetworkSettings", {}).get("Networks", {}):
            network.disconnect(backend)
        network.remove()
