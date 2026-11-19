"""VMコンテナ管理 — デバッグ用の作り直し（再起動）機能。

backend コンテナにマウントされた Docker ソケット経由で、
同じ Compose プロジェクトの `vm` サービスを操作する。
Docker が利用できない環境では DockerUnavailableError を送出し、
API 層で 503 として返す。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from ai_desktop_agent.server.docker_client import DockerClientOwner, docker_operation
from ai_desktop_agent.server.docker_client import DockerUnavailableError as DockerUnavailableError
from ai_desktop_agent.server.vm_locks import environment_lock

logger = logging.getLogger(__name__)

SERVICE_LABEL = "com.docker.compose.service"
PROJECT_LABEL = "com.docker.compose.project"
VM_SERVICE = "vm"


@dataclass
class VmStatusInfo:
    """VMコンテナの状態。"""

    running: bool
    status: str  # running / exited / restarting / not_found 等
    health: str | None = None  # healthy / unhealthy / starting / none
    name: str | None = None
    qmp_status: str | None = None  # QMP query-status の status（inmigrate等もあり得る）


class VmController(DockerClientOwner):
    """`vm` サービスコンテナの状態取得・再起動を行う。

    Args:
        container_name: 明示的なコンテナ名（VM_CONTAINER_NAME 環境変数）。
            未指定時は Compose ラベルから `vm` サービスを探索する。
        client: テスト用の差し替え可能な Docker クライアント。
    """

    _connection_error = (
        "Docker ソケットに接続できません。/var/run/docker.sock のマウントを確認してください"
    )

    def __init__(
        self,
        container_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._container_name = container_name or os.environ.get("VM_CONTAINER_NAME")
        super().__init__(client)

    # ── 公開 API ──────────────────────────────────────

    @docker_operation
    def status(self) -> VmStatusInfo:
        """VMコンテナの現在の状態を返す。QMPが生きていればゲスト状態も付与。"""
        container = self._find_container()
        if container is None:
            return VmStatusInfo(running=False, status="not_found")
        return self._inspect_status(container)

    def _inspect_status(self, container: Any) -> VmStatusInfo:
        from docker.errors import NotFound

        try:
            container.reload()
            state = container.attrs.get("State") or {}
            info = VmStatusInfo(
                running=bool(state.get("Running")),
                status=str(container.status),
                health=(state.get("Health") or {}).get("Status"),
                name=container.name,
            )
        except NotFound:
            return VmStatusInfo(running=False, status="not_found")
        except Exception as error:
            raise DockerUnavailableError(
                "VMの状態を確認できません。Dockerの接続と権限を確認してください"
            ) from error
        info.qmp_status = self._qmp_status()
        return info

    @docker_operation
    def target_vm_id(self) -> str:
        container = self._find_container()
        if container is None:
            raise DockerUnavailableError("vm コンテナが見つかりません")
        return self._environment_id(container)

    @staticmethod
    def _environment_id(container: Any) -> str:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        service = labels.get(SERVICE_LABEL)
        return (
            service
            if service in (VM_SERVICE, "desktop", "android")
            else labels.get("ai-desktop-agent.vm-id") or VM_SERVICE
        )

    @docker_operation
    def restart(self, timeout: int = 30, *, expected_vm_id: str | None = None) -> VmStatusInfo:
        """VMコンテナを再起動する（ゲストOSを含む再起動）。

        KVMが利用できない環境ではTCG実行が遅すぎるため再起動を制限する
        （`KvmUnavailableError`）。デバッグ用の明示許可は `ALLOW_TCG_VM=true`。
        `docker restart` はコンテナ再起動の完了で戻る。
        ゲストのデスクトップが使えるようになるまで数分かかる。
        """
        container = self._find_container()
        if container is None:
            raise DockerUnavailableError("vm コンテナが見つかりません")
        vm_id = self._environment_id(container)
        if expected_vm_id is not None and expected_vm_id != vm_id:
            raise DockerUnavailableError("再起動の対象が変わりました。状態を確認してください")
        with environment_lock(vm_id):
            return self._restart(container, timeout)

    def _restart(self, container: Any, timeout: int) -> VmStatusInfo:
        from ai_desktop_agent.server.kvm import (
            KvmUnavailableError,
            is_kvm_available,
            is_tcg_allowed,
        )

        if not is_tcg_allowed() and not is_kvm_available(self._client):
            raise KvmUnavailableError()
        logger.info("VMコンテナを再起動します: %s", container.name)
        try:
            container.restart(timeout=timeout)
        except Exception as error:
            raise DockerUnavailableError(
                "VMの再起動を確認できません。状態を確認してから操作してください"
            ) from error
        return self._inspect_status(container)

    # ── 内部 ──────────────────────────────────────────

    def _qmp_status(self) -> str | None:
        """QMPでゲスト稼働状態を照会する。失敗時は None。"""
        try:
            from ai_desktop_agent.vm.qmp import QmpClient

            sock = os.environ.get("QMP_SOCK", "/vm/sockets/qmp.sock")
            with QmpClient(sock, timeout=3.0) as qmp:
                return str(qmp.query_status().get("status"))
        except Exception:
            logger.debug("QMP照会に失敗", exc_info=True)
            return None

    def _find_container(self) -> Any | None:
        client = self._docker()
        if self._container_name:
            from docker.errors import NotFound

            try:
                return client.containers.get(self._container_name)
            except NotFound:
                logger.warning("VMコンテナが見つかりません: %s", self._container_name)
                return None
            except Exception as error:
                raise DockerUnavailableError(
                    "VMを確認できません。Dockerの接続と権限を確認してください"
                ) from error
        from ai_desktop_agent.server.vm_pool import VmPool

        candidates = VmPool(client=client)._fixed_containers(client, VM_SERVICE)
        return VmPool._pick_fixed(candidates, VM_SERVICE)


def get_vm_controller() -> VmController:
    """デフォルトの VmController を生成する（テストで差し替え可能）。"""
    return VmController()
