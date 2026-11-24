"""Prepare Compose updates before it can replace an environment's writable layer."""

import argparse
import contextlib
import logging
import os
import signal
import socket
import sys
from pathlib import Path

from docker.errors import NotFound

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.factory import validate_llm_configuration
from ai_desktop_agent.server.desktop_resources import DesktopResources, desktop_runtime_from_env
from ai_desktop_agent.server.networks import EnvironmentNetworks
from ai_desktop_agent.server.persistence import metadata_lock
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import KIND_CONTAINER, VmPool
from ai_desktop_agent.server.workspaces import HomeBackups, WorkspaceVolumes

logger = logging.getLogger(__name__)


class WorkspaceUpgrade:
    def __init__(self, pool=None, root=None, backend=None):
        self._owns_pool = pool is None
        self.pool = VmPool() if pool is None else pool
        try:
            self.client = self.pool._docker()
            self.project = self.pool._project_name(self.client)
            self.backend = backend or self.client.containers.get(socket.gethostname())
            if self.pool._labels(self.backend).get("com.docker.compose.project") != self.project:
                raise DockerUnavailableError("更新処理の所属プロジェクトを確認できません")
            self.root = Path(root or os.environ.get("DATA_DIR", "/app/data"))
            self.volumes = WorkspaceVolumes(self.client, self.project, self.backend.attrs["Image"])
            self.backups = HomeBackups(self.root, self.project)
            self.stopped = []
        except BaseException:
            self._close_pool()
            raise

    def _close_pool(self):
        if self._owns_pool:
            self.pool.close()

    def _stop(self, container):
        container.reload()
        if container.status in {"running", "paused", "restarting"}:
            self.stopped.append((container.id, container.status == "paused"))
            if container.status == "paused":
                container.unpause()
            container.stop(timeout=30)

    def _rollback(self):
        for container_id, paused in reversed(self.stopped):
            try:
                container = self.client.containers.get(container_id)
                container.reload()
                if container.status not in {"running", "paused"}:
                    container.start()
                if paused and container.status != "paused":
                    container.pause()
            except NotFound:
                # Compose may already have replaced it; its verified backup remains available.
                continue
            except Exception:
                logger.exception("更新前のコンテナを再開できませんでした: %s", container_id)

    @contextlib.contextmanager
    def transaction(self):
        # The CLI holds this lock until the calling shell finishes `compose up`.
        try:
            with metadata_lock(self.root / f"workspace-update-{self.project}.lock"):
                self.stopped = []
                try:
                    self.volumes.reap_helpers()
                    yield self
                except BaseException:
                    self._rollback()
                    raise
        finally:
            self._close_pool()

    def prepare(self, profile):
        service = "desktop" if profile == "container" else "vm"
        sources = self.pool._fixed_containers(self.client, service)
        if len(sources) > 1:
            raise DockerUnavailableError("更新する固定デスクトップを1つに絞れません")
        for backend in self.pool._fixed_containers(self.client, "backend"):
            if (
                backend.id != self.backend.id
                and self.pool._labels(backend).get("com.docker.compose.oneoff", "false").lower()
                != "true"
            ):
                self._stop(backend)
        for source in sources:
            self._stop(source)
            if service != "desktop":
                self._reconcile_network(service)
                continue
            name = self.backups.capture(source, "desktop")
            logger.info("更新前のホームを保存しました: %s", name)
            if not self.volumes.mounted(source, "desktop"):
                volume = self.volumes.ensure("desktop", seed_source=source.id)
                self.backups.seed(self.volumes, volume, name)
            self.pool._save_state(
                "desktop",
                {"home_volume": self.volumes.name_for("desktop"), "last_home_backup": name},
            )
            self._reconcile_network("desktop")

    def _reconcile_network(self, vm_id):
        """Upgrade old runtime-created bridge labels without stranding stopped containers."""
        manager = EnvironmentNetworks(self.client, self.project)
        name = manager.name_for(vm_id)
        try:
            network = self.client.networks.get(name)
        except NotFound:
            return
        if not manager.owned(network, vm_id):
            raise DockerUnavailableError("更新対象のネットワークの所属を確認できません")
        old_labels = network.attrs.get("Labels") or {}
        key = "com.docker.compose.network"
        if old_labels.get(key) == f"env_{vm_id}":
            return
        members = [
            c
            for c in self.client.containers.list(all=True)
            if name in (c.attrs.get("NetworkSettings") or {}).get("Networks", {})
        ]
        allowed = {
            c.id
            for service in (vm_id, "backend")
            for c in self.pool._fixed_containers(self.client, service)
            if self.pool._labels(c).get("com.docker.compose.oneoff", "false").lower() != "true"
        }
        if any(c.id not in allowed for c in members):
            raise DockerUnavailableError(
                "ネットワークに別のコンテナが接続しているため更新を止めました"
            )
        if any(c.status in {"running", "paused", "restarting"} for c in members):
            raise DockerUnavailableError("環境を停止してからネットワークを更新してください")
        endpoints = [(c, c.attrs["NetworkSettings"]["Networks"][name]) for c in members]
        config = {
            "driver": "bridge",
            "ipam": network.attrs.get("IPAM"),
            "internal": network.attrs.get("Internal", False),
            "enable_ipv6": network.attrs.get("EnableIPv6", False),
            "options": network.attrs.get("Options") or {},
        }
        try:
            for container, _ in endpoints:
                network.disconnect(container)
            network.remove()
            self.client.networks.create(
                name,
                labels={
                    **old_labels,
                    "com.docker.compose.project": self.project,
                    key: f"env_{vm_id}",
                },
                **config,
            )
        finally:
            try:
                network = self.client.networks.get(name)
            except NotFound:
                network = self.client.networks.create(name, labels=old_labels, **config)
            failures = []
            for container, endpoint in endpoints:
                try:
                    container.reload()
                    if name not in (container.attrs.get("NetworkSettings") or {}).get(
                        "Networks", {}
                    ):
                        ipam = endpoint.get("IPAMConfig") or {}
                        network.connect(
                            container,
                            aliases=endpoint.get("Aliases"),
                            ipv4_address=ipam.get("IPv4Address"),
                            ipv6_address=ipam.get("IPv6Address"),
                        )
                except Exception as error:
                    # Restore the other members even if one endpoint cannot be inspected
                    # or reconnected. Keep every failure available to the operator.
                    error.add_note(f"container={container.id} network={name}")
                    failures.append(error)
            if failures:
                raise DockerUnavailableError(
                    "更新後のネットワークへの再接続に失敗したコンテナがあります"
                ) from ExceptionGroup("workspace network reconnection failed", failures)

    def backup(self, vm_id):
        info = self.pool.get_vm(vm_id)
        if info is None or info.kind != KIND_CONTAINER:
            raise ValueError("バックアップ対象のデスクトップが見つかりません")
        targets = self.pool._lifecycle_targets(vm_id, self.client, include_relay=False)
        if len(targets) != 1:
            raise ValueError("バックアップ対象を1つに絞れません")
        source = targets[0]
        source.reload()
        if source.status not in {"running", "paused", "exited", "created"}:
            raise DockerUnavailableError(
                "環境の状態が安定していません。起動・停止の完了後にバックアップしてください"
            )
        running = source.status == "running"
        failure = None
        name = None
        try:
            if running:
                # The daemon may pause the guest even if its response is lost.
                source.pause()
            name = self.backups.capture(source, vm_id)
            return name
        except BaseException as error:
            failure = error
            raise
        finally:
            if running:
                try:
                    try:
                        source.unpause()
                    except Exception:
                        # A lost response or a rejected pause can mean it is already running.
                        source.reload()
                        if source.status != "running":
                            raise
                except BaseException as error:
                    error.add_note(f"バックアップ後の環境再開に失敗: {vm_id} ({source.id})")
                    if name is not None:
                        error.add_note(f"保存済みバックアップ: {name}")
                    if failure is not None:
                        raise BaseExceptionGroup(
                            "バックアップと環境の再開に失敗しました", [failure, error]
                        ) from None
                    raise

    def restore(self, name):
        _, metadata, _ = self.backups.verified(name)
        volume = self.volumes.ensure(metadata["vm_id"])
        self.volumes.require_unused(volume)
        # Keep an undo archive even when restoring an unused volume left by `compose rm`.
        with self.volumes.helper(volume, readonly=True) as helper:
            undo = self.backups.capture(helper, metadata["vm_id"])
        logger.info("復元前のホームを保存しました: %s", undo)
        self.backups.seed(self.volumes, volume, name)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--profile", choices=["container", "kvm"], required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("vm_id")
    restore = commands.add_parser("restore")
    restore.add_argument("backup_id")
    args = parser.parse_args()

    def interrupt(*_):
        raise KeyboardInterrupt

    if args.command == "prepare":
        # Check the new backend's effective environment before stopping a working desktop.
        try:
            validate_llm_configuration()
            ExecutionLimits.from_env()
            DesktopResources.from_env()
            desktop_runtime_from_env()
        except ValueError as error:
            parser.exit(2, f"設定を確認してください: {error}\n")

    signal.signal(signal.SIGTERM, interrupt)
    upgrade = WorkspaceUpgrade()
    with upgrade.transaction():
        if args.command == "prepare":
            upgrade.prepare(args.profile)
            print("WORKSPACE_READY", flush=True)
            if sys.stdin.readline().strip() != "commit":
                raise RuntimeError("更新を完了できませんでした。元の環境を再開します")
        elif args.command == "backup":
            print(upgrade.backup(args.vm_id), flush=True)
        else:
            upgrade.restore(args.backup_id)


if __name__ == "__main__":
    main()
