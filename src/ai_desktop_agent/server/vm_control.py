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

logger = logging.getLogger(__name__)

SERVICE_LABEL = "com.docker.compose.service"
PROJECT_LABEL = "com.docker.compose.project"
VM_SERVICE = "vm"


class DockerUnavailableError(RuntimeError):
    """Docker ソケットに接続できない場合のエラー。"""


@dataclass
class VmStatusInfo:
    """VMコンテナの状態。"""

    running: bool
    status: str  # running / exited / restarting / not_found 等
    health: str | None = None  # healthy / unhealthy / starting / none
    name: str | None = None


class VmController:
    """`vm` サービスコンテナの状態取得・再起動を行う。

    Args:
        container_name: 明示的なコンテナ名（VM_CONTAINER_NAME 環境変数）。
            未指定時は Compose ラベルから `vm` サービスを探索する。
        client: テスト用の差し替え可能な Docker クライアント。
    """

    def __init__(
        self,
        container_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._container_name = container_name or os.environ.get("VM_CONTAINER_NAME")
        self._client = client

    # ── 公開 API ──────────────────────────────────────

    def status(self) -> VmStatusInfo:
        """VMコンテナの現在の状態を返す。"""
        container = self._find_container()
        if container is None:
            return VmStatusInfo(running=False, status="not_found")
        container.reload()
        state = container.attrs.get("State", {})
        health = (state.get("Health") or {}).get("Status")
        return VmStatusInfo(
            running=bool(state.get("Running")),
            status=str(container.status),
            health=health,
            name=container.name,
        )

    def restart(self, timeout: int = 30) -> VmStatusInfo:
        """VMコンテナを再起動する（ゲストOSごと作り直し）。

        `docker restart` はコンテナ再起動の完了で戻る。
        ゲストのデスクトップが使えるようになるまで数分かかる。
        """
        container = self._find_container()
        if container is None:
            raise DockerUnavailableError("vm コンテナが見つかりません")
        logger.info("VMコンテナを再起動します: %s", container.name)
        container.restart(timeout=timeout)
        return self.status()

    # ── 内部 ──────────────────────────────────────────

    def _docker(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import docker
        except ImportError as e:
            raise DockerUnavailableError("docker パッケージが利用できません") from e
        try:
            client = docker.from_env()
            # 接続確認（ソケット未マウント時はここで失敗する）
            client.ping()
        except Exception as e:
            raise DockerUnavailableError(
                "Docker ソケットに接続できません。/var/run/docker.sock のマウントを確認してください"
            ) from e
        self._client = client
        return client

    def _find_container(self) -> Any | None:
        client = self._docker()
        if self._container_name:
            try:
                return client.containers.get(self._container_name)
            except Exception:
                logger.warning("VMコンテナが見つかりません: %s", self._container_name)
                return None
        project = os.environ.get("COMPOSE_PROJECT_NAME")
        candidates = client.containers.list(
            all=True, filters={"label": f"{SERVICE_LABEL}={VM_SERVICE}"}
        )
        if project:
            for c in candidates:
                labels = (c.attrs.get("Config") or {}).get("Labels") or {}
                if labels.get(PROJECT_LABEL) == project:
                    return c
            return None
        return candidates[0] if candidates else None


def get_vm_controller() -> VmController:
    """デフォルトの VmController を生成する（テストで差し替え可能）。"""
    return VmController()
