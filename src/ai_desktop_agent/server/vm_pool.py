"""動的VMプール — 複数VMの作成・削除・発見。

各環境は専用コンテナで動く。QEMUはbaseの独立したコピーを使う。
ブラウザの画面配信はバックエンドの認証付きVNCプロキシを通す。

compose の `vm` サービスは id `vm` として自動検出する。
Docker が使えない環境では DockerUnavailableError を送出する。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ai_desktop_agent.server.networks import (  # noqa: E402
    LABEL_PROJECT,
    LABEL_VM_ID,
    EnvironmentNetworks,
)
from ai_desktop_agent.server.persistence import (  # noqa: E402
    StorageError,
    atomic_json,
    metadata_lock,
)
from ai_desktop_agent.server.vm_control import DockerUnavailableError  # noqa: E402
from ai_desktop_agent.server.workspaces import HOME, WorkspaceVolumes  # noqa: E402

LABEL_WS_FOR = "ai-desktop-agent.ws-for"
LABEL_KIND = "ai-desktop-agent.kind"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

KIND_QEMU = "qemu"
KIND_CONTAINER = "container"
KIND_ANDROID = "android"
#: 動的環境IDの接頭辞
ID_PREFIX = {KIND_QEMU: "vm", KIND_CONTAINER: "desk", KIND_ANDROID: "and"}

BASE_VM_SERVICE = "vm"
#: 軽量コンテナ実行環境（QEMU不要のVNCデスクトップ）。既定の作業環境。
DESKTOP_SERVICE = "desktop"
#: 固定Android端末（Android-x86 on QEMU。kvmプロファイル時のみ存在）
ANDROID_SERVICE = "android"
#: 固定サービスに対応する中継（websockify）サービス名。停止/起動は中継も連動する。
FIXED_RELAY_SERVICE = {
    BASE_VM_SERVICE: "websockify",
    DESKTOP_SERVICE: "websockify-desktop",
    ANDROID_SERVICE: "websockify-android",
}
OVERLAYS_DIR = "/vm/overlays"  # backendコンテナ内のマウント先
BASE_IMAGE_IN_VM = "/vm/desktop.qcow2"  # vmコンテナ内から見たbase
MAX_VMS = 8


def _discovery_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (DockerUnavailableError, StorageError):
            raise
        except Exception as error:
            raise DockerUnavailableError(
                "作業環境を確認できません。Dockerの状態と権限を確認してください"
            ) from error

    return guarded


@dataclasses.dataclass
class VmInfo:
    """1台のVMの情報。"""

    id: str
    name: str
    status: str  # running / exited / creating / not_found
    health: str | None = None
    vnc_port: int = 0  # 旧API互換。接続は /vnc/{id} を使う
    ws_port: int = 0  # 旧API互換。外部ポートを払い出さない
    vnc_host: str = ""  # backend からの接続先ホスト名
    managed: bool = True  # False: compose管理の既定VM・desktop
    kind: str = KIND_QEMU  # KIND_QEMU / KIND_CONTAINER / KIND_ANDROID
    adb_port: int = 0  # 旧API互換。ADBはDocker内部の5555を使う

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class VmPool:
    """VMコンテナ群のライフサイクル管理。

    Args:
        client: テスト用の差し替え可能な Docker クライアント。
        state_file: 環境情報の永続化先。省略時は `$DATA_DIR/vms.json`。
        overlays_dir: overlay qcow2 の保存先（backendコンテナ内パス）。
    """

    def __init__(
        self,
        client: Any | None = None,
        state_file: str | Path | None = None,
        overlays_dir: str = OVERLAYS_DIR,
    ) -> None:
        self._client = client
        base = os.environ.get("DATA_DIR", "./data")
        self._state_file = Path(state_file or Path(base) / "vms.json")
        self._overlays_dir = overlays_dir

    # ── 一覧・取得 ──────────────────────────────────

    def isolate_environments(self) -> None:
        # Snapshot discovery before detaching old shared networks used for legacy adoption.
        environments = self.list_vms()
        for vm in environments:
            self.prepare_vm(vm.id)

    def prepare_vm(self, vm_id: str) -> None:
        docker = self._docker()
        networks = EnvironmentNetworks(docker, self._project_name(docker))
        for target in self._lifecycle_targets(vm_id, docker, include_relay=False):
            network = networks.ensure(vm_id, target)
            state = self._load_state(vm_id) or {}
            if state.get("isolated_network") != network.name:
                # Keep legacy environments discoverable even if a later migration step fails.
                self._save_state(vm_id, {**state, "isolated_network": network.name})
            networks.isolate(vm_id, target)

    def retire_legacy_relays(self) -> None:
        """Remove this project's unauthenticated relays before exposing the authenticated API."""
        docker = self._docker()
        targets = []
        for service in FIXED_RELAY_SERVICE.values():
            targets.extend(self._fixed_containers(docker, service))
        targets.extend(
            c for c in self._dynamic_containers(docker) if LABEL_WS_FOR in self._labels(c)
        )
        for container in targets:
            container.remove(force=True)
            logger.info("旧画面配信の中継を削除: %s", container.name)

    def _project_name(self, docker: Any) -> str:
        project = os.environ.get("COMPOSE_PROJECT_NAME")
        if not project:
            try:
                backend = docker.containers.get(socket.gethostname())
                project = self._labels(backend).get("com.docker.compose.project")
            except Exception:
                pass
        return project or "ai-desktop-agent"

    @_discovery_operation
    def _fixed_containers(self, docker: Any, service: str) -> list[Any]:
        """Find fixed services only in this backend's Compose project."""
        project = self._project_name(docker)
        candidates = docker.containers.list(
            all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={service}"}
        )
        return [
            c for c in candidates if self._labels(c).get("com.docker.compose.project") == project
        ]

    @_discovery_operation
    def _dynamic_containers(self, docker: Any, vm_id: str | None = None) -> list[Any]:
        label = LABEL_VM_ID if vm_id is None else f"{LABEL_VM_ID}={vm_id}"
        candidates = docker.containers.list(all=True, filters={"label": label})
        project = self._project_name(docker)
        states = self._load_all_states()
        network = self._backend_network(docker)
        owned = []
        for container in candidates:
            labels = self._labels(container)
            owner = labels.get(LABEL_PROJECT)
            if owner is not None:
                if owner == project:
                    owned.append(container)
                continue
            # Legacy containers have no owner label. Require this store's allocation
            # AND this backend's network; a shared Docker daemon alone proves nothing.
            nets = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
            if labels.get(LABEL_VM_ID) in states and network and network in nets:
                owned.append(container)
                continue
            vm_key = labels.get(LABEL_VM_ID, "")
            isolated = (states.get(vm_key) or {}).get("isolated_network")
            manager = EnvironmentNetworks(docker, project)
            if (
                isolated
                and isolated in nets
                and isolated == manager.name_for(vm_key)
                and manager.owned(docker.networks.get(isolated), vm_key)
            ):
                owned.append(container)
        return owned

    @staticmethod
    def _pick_fixed(candidates: list, service: str) -> Any | None:
        """固定サービスのコンテナを1つ選ぶ。

        同一ラベルを持つ手動コンテナが混ざると影武者になる
        （例: android-nowiz が android を乗っ取り、VNC接続先が到達不能に）。
        compose命名（ai-desktop-agent-<service>-N）に一致するものを優先し、
        なければ先頭を返す。
        """
        if not candidates:
            return None
        prefix = f"ai-desktop-agent-{service}-"
        for c in candidates:
            if getattr(c, "name", "").startswith(prefix):
                return c
        return candidates[0]

    def list_vms(self) -> list[VmInfo]:
        """実行中・停止中の全VMを返す（compose既定VM・desktopを含む）。"""
        docker = self._docker()
        found: dict[str, VmInfo] = {}

        # compose管理の既定VM（サービス名 vm。kvmプロファイル時のみ存在）
        legacy = self._fixed_containers(docker, BASE_VM_SERVICE)
        picked = self._pick_fixed(legacy, BASE_VM_SERVICE)
        if picked is not None:
            found[BASE_VM_SERVICE] = self._from_container(
                picked, BASE_VM_SERVICE, managed=False, kind=KIND_QEMU
            )

        # compose管理の軽量コンテナ環境（サービス名 desktop。containerプロファイル時）
        desktops = self._fixed_containers(docker, DESKTOP_SERVICE)
        picked = self._pick_fixed(desktops, DESKTOP_SERVICE)
        if picked is not None:
            found[DESKTOP_SERVICE] = self._from_container(
                picked, DESKTOP_SERVICE, managed=False, kind=KIND_CONTAINER
            )

        # compose管理の固定Android端末（サービス名 android。kvmプロファイル時のみ存在）
        androids = self._fixed_containers(docker, ANDROID_SERVICE)
        picked = self._pick_fixed(androids, ANDROID_SERVICE)
        if picked is not None:
            found[ANDROID_SERVICE] = self._from_container(
                picked, ANDROID_SERVICE, managed=False, kind=KIND_ANDROID
            )

        # 動的VM
        dynamic = self._dynamic_containers(docker)
        for c in dynamic:
            if LABEL_WS_FOR in self._labels(c):
                continue
            vm_id = self._labels(c).get(LABEL_VM_ID, "")
            if vm_id and vm_id != BASE_VM_SERVICE:
                found[vm_id] = self._from_container(c, vm_id, managed=True)

        return [found[k] for k in sorted(found)]

    def get_vm(self, vm_id: str) -> VmInfo | None:
        """1台取得。なければ None。"""
        for vm in self.list_vms():
            if vm.id == vm_id:
                return vm
        return None

    def default_vm(self) -> VmInfo | None:
        """タスク投入先の既定VM（稼働中の最初の1台）。なければ None。

        compose管理の `desktop` が稼働していれば優先する。
        """
        running = [vm for vm in self.list_vms() if vm.status == "running"]
        for vm in running:
            if vm.id == DESKTOP_SERVICE:
                return vm
        return running[0] if running else None

    # ── 作成・削除 ──────────────────────────────────

    def create_vm(self, name: str | None = None, kind: str = KIND_QEMU) -> VmInfo:
        """新しい専用環境を作成する。公開ポートと中継は作らない。

        kind=qemu: QEMU VM（専用ディスクとコンテナ）。KVM必須のため、KVMが
        利用できない環境では作成を制限する（`KvmUnavailableError`）。
        kind=container: 軽量デスクトップコンテナ。KVM不要でどこでも作れる。
        kind=android: Android-x86のQEMU起動。KVM必須。
        """
        from ai_desktop_agent.server.kvm import (
            KvmUnavailableError,
            is_kvm_available,
            is_tcg_allowed,
            resolve_use_kvm,
        )

        if kind not in (KIND_QEMU, KIND_CONTAINER, KIND_ANDROID):
            raise ValueError(f"kind は qemu/container/android のいずれか: {kind}")

        docker = self._docker()
        if (
            kind in (KIND_QEMU, KIND_ANDROID)
            and not is_tcg_allowed()
            and not is_kvm_available(docker)
        ):
            raise KvmUnavailableError()
        if len([v for v in self.list_vms() if v.managed]) >= MAX_VMS:
            raise ValueError(f"VM数の上限に達しています（{MAX_VMS}）")

        prefix = ID_PREFIX[kind]
        vm_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        vnc_port = ws_port = 0

        common_labels = {
            LABEL_VM_ID: vm_id,
            LABEL_KIND: kind,
            LABEL_PROJECT: self._project_name(docker),
        }
        vm_name = f"ai-desktop-agent-{vm_id}"
        logger.info("VM作成: %s kind=%s (vnc=%d ws=%d)", vm_id, kind, vnc_port, ws_port)

        allocation = {
            "vnc_port": vnc_port,
            "ws_port": ws_port,
            "name": name or vm_id,
            "isolated_network": EnvironmentNetworks(docker, self._project_name(docker)).name_for(
                vm_id
            ),
        }
        if kind == KIND_CONTAINER:
            allocation["home_volume"] = WorkspaceVolumes(
                docker, self._project_name(docker)
            ).name_for(vm_id)
        with self._creation(docker, vm_id, allocation):
            network = EnvironmentNetworks(docker, self._project_name(docker)).ensure(vm_id).name
            adb_port = 0
            if kind == KIND_ANDROID:
                iso_host = self._android_iso_host()
                if iso_host is None:
                    raise ValueError(
                        "Android ISO がありません。android/README.md の手順で取得してください"
                    )
                image = self._android_image_ref(docker)
                # 未設定時は entrypoint 側で /dev/kvm の有無から自動切替する
                use_kvm = resolve_use_kvm()
                memory = os.environ.get("ANDROID_MEMORY", "2048")
                cpus = os.environ.get("ANDROID_CPUS", "4")
                # ADBはDocker内部の5555に接続する。ホストには公開しない。
                docker.containers.run(
                    image,
                    name=vm_name,
                    detach=True,
                    cap_drop=["NET_RAW"],
                    # TCGの明示許可時はKVMデバイスを要求しない。
                    devices=["/dev/kvm:/dev/kvm"] if is_kvm_available(docker) else [],
                    environment={
                        "ANDROID_ISO": "/android/boot.iso",
                        "ANDROID_MEMORY": memory,
                        "ANDROID_CPUS": cpus,
                        "ANDROID_VNC_PORT": "5900",
                        "ANDROID_ADB_FORWARD": "1",
                        "ANDROID_ADB_PORT": "5555",
                        "USE_KVM": use_kvm,
                        "ALLOW_TCG_VM": os.environ.get("ALLOW_TCG_VM", "false"),
                    },
                    volumes={
                        iso_host: {"bind": "/android/boot.iso", "mode": "ro"},
                        **self._android_ro_files(),
                    },
                    labels=common_labels,
                    network=network,
                    restart_policy={"Name": "unless-stopped"},
                )
                self._save_state(
                    vm_id,
                    {
                        "vnc_port": vnc_port,
                        "ws_port": ws_port,
                        "adb_port": adb_port,
                        "name": name or vm_id,
                    },
                )
                return VmInfo(
                    id=vm_id,
                    name=name or vm_id,
                    status="creating",
                    vnc_port=vnc_port,
                    ws_port=ws_port,
                    vnc_host=vm_name,
                    managed=True,
                    kind=kind,
                    adb_port=adb_port,
                )

            if kind == KIND_QEMU:
                overlay_host = self._create_overlay(vm_id)
                image = self._vm_image_ref(docker)
                # 未設定時は entrypoint 側で /dev/kvm の有無から自動切替する
                use_kvm = resolve_use_kvm()
                memory = os.environ.get("VM_MEMORY", "4096")
                cpus = os.environ.get("VM_CPUS", "4")

                host_repo = self._host_repo_dir()
                volumes = {
                    overlay_host: {"bind": f"/vm/overlays/{vm_id}.qcow2", "mode": "rw"},
                    f"{host_repo}/vm/sockets": {"bind": "/vm/sockets", "mode": "rw"},
                    **self._ro_vm_files(),
                }
                docker.containers.run(
                    image,
                    name=vm_name,
                    detach=True,
                    cap_drop=["NET_RAW"],
                    # TCGの明示許可時はKVMデバイスを要求しない。
                    devices=["/dev/kvm:/dev/kvm"] if is_kvm_available(docker) else [],
                    environment={
                        "VM_MEMORY": memory,
                        "VM_CPUS": cpus,
                        "VM_VNC_PORT": "5900",
                        "VM_IMAGE": f"/vm/overlays/{vm_id}.qcow2",
                        "CMDLINE_FILE": "/vm/cmdline.txt",
                        "USE_KVM": use_kvm,
                        "ALLOW_TCG_VM": os.environ.get("ALLOW_TCG_VM", "false"),
                        "QMP_SOCK": f"/vm/sockets/{vm_id}-qmp.sock",
                        "QGA_SOCK": f"/vm/sockets/{vm_id}-qga.sock",
                    },
                    volumes=volumes,
                    labels=common_labels,
                    network=network,
                    restart_policy={"Name": "unless-stopped"},
                )
            else:
                image = self._desktop_image_ref(docker)
                home = WorkspaceVolumes(docker, self._project_name(docker)).ensure(vm_id)
                docker.containers.run(
                    image,
                    name=vm_name,
                    detach=True,
                    cap_drop=["NET_RAW"],
                    shm_size="1g",
                    volumes={home.name: {"bind": HOME, "mode": "rw"}},
                    environment={
                        "LANG": "C.UTF-8",
                        "VNC_GEOMETRY": os.environ.get("VNC_GEOMETRY", "1280x800"),
                        "VNC_DEPTH": os.environ.get("VNC_DEPTH", "24"),
                    },
                    labels=common_labels,
                    network=network,
                    restart_policy={"Name": "unless-stopped"},
                )

            return self._finish_create(vm_id, name, vm_name, vnc_port, ws_port, kind)

    def _finish_create(
        self,
        vm_id: str,
        name: str | None,
        vm_name: str,
        vnc_port: int,
        ws_port: int,
        kind: str,
    ) -> VmInfo:
        """作成状態を保存し、作成中情報を返す。"""
        self._save_state(vm_id, {"vnc_port": vnc_port, "ws_port": ws_port, "name": name or vm_id})
        return VmInfo(
            id=vm_id,
            name=name or vm_id,
            status="creating",
            vnc_port=vnc_port,
            ws_port=ws_port,
            vnc_host=vm_name,
            managed=True,
            kind=kind,
        )

    def restart_vm(self, vm_id: str, timeout: int = 30) -> VmInfo:
        """VMコンテナを再起動する（ゲストOSを含む再起動）。中継は触らない。

        QEMU系（VM・Android）はKVM必須のため、KVMが利用できない環境では
        再起動を制限する（`KvmUnavailableError`）。
        コンテナ環境の再起動はどこでもできる。
        """
        from ai_desktop_agent.server.kvm import (
            KvmUnavailableError,
            is_kvm_available,
            is_tcg_allowed,
        )

        docker = self._docker()
        targets = self._lifecycle_targets(vm_id, docker, include_relay=False)
        if not targets:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        if (
            self._target_kind(vm_id, docker) in (KIND_QEMU, KIND_ANDROID)
            and not is_tcg_allowed()
            and not is_kvm_available(docker)
        ):
            raise KvmUnavailableError()
        for c in targets:
            c.restart(timeout=timeout)
            logger.info("VM再起動: %s", c.name)
        info = self.get_vm(vm_id)
        if info is None:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        return info

    def stop_vm(self, vm_id: str, timeout: int = 10) -> VmInfo:
        """VM/コンテナを停止する（固定・動的とも可）。中継も連動して止める。

        停止済みの場合は何もせず現在情報を返す。存在しなければ ValueError。
        """
        docker = self._docker()
        targets = self._lifecycle_targets(vm_id, docker, include_relay=True)
        if not targets:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        for c in targets:
            try:
                c.stop(timeout=timeout)
            except Exception as e:
                raise DockerUnavailableError(f"環境の停止に失敗しました: {c.name}") from e
            logger.info("VM停止: %s", c.name)
        info = self.get_vm(vm_id)
        if info is None:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        return info

    def start_vm(self, vm_id: str) -> VmInfo:
        """VM/コンテナを起動する（固定・動的とも可）。中継も連動して起動する。

        QEMU系（VM・Android）はKVM必須のため、KVMが利用できない環境では
        起動を制限する（`KvmUnavailableError`）。
        存在しなければ ValueError。
        """
        from ai_desktop_agent.server.kvm import (
            KvmUnavailableError,
            is_kvm_available,
            is_tcg_allowed,
        )

        docker = self._docker()
        targets = self._lifecycle_targets(vm_id, docker, include_relay=True)
        if not targets:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        if (
            self._target_kind(vm_id, docker) in (KIND_QEMU, KIND_ANDROID)
            and not is_tcg_allowed()
            and not is_kvm_available(docker)
        ):
            raise KvmUnavailableError()
        for c in targets:
            try:
                c.start()
            except Exception as e:
                raise DockerUnavailableError(f"環境の起動に失敗しました: {c.name}") from e
            logger.info("VM起動: %s", c.name)
        info = self.get_vm(vm_id)
        if info is None:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        return info

    def _lifecycle_targets(self, vm_id: str, docker: Any, include_relay: bool) -> list[Any]:
        """停止/起動/再起動の対象コンテナを返す。

        固定サービス（vm/desktop/android）は compose サービスラベルで探す。
        include_relay=True の場合は対応する websockify 中継も含める。
        動的VMは LABEL_VM_ID で探し、include_relay=False では中継を除く。
        """
        if vm_id in (BASE_VM_SERVICE, DESKTOP_SERVICE, ANDROID_SERVICE):
            mains = self._fixed_containers(docker, vm_id)
            if not include_relay:
                return list(mains)
            relays: list[Any] = []
            relay_service = FIXED_RELAY_SERVICE.get(vm_id)
            if relay_service:
                relays = self._fixed_containers(docker, relay_service)
            return [*list(mains), *list(relays)]
        all_targets = self._dynamic_containers(docker, vm_id)
        if include_relay:
            return list(all_targets)
        return [c for c in all_targets if LABEL_WS_FOR not in self._labels(c)]

    def remove_vm(self, vm_id: str) -> bool:
        """VM/コンテナを削除する（固定・動的とも可）。

        固定サービス（vm/desktop/android）は本体＋中継コンテナを停止・削除する
        （`docker compose up` で復元できる。ディスクイメージは消さない）。
        注意: 固定androidと同一composeラベルを持つ手動コンテナ
        （`android-nowiz` 等）も対象になる。
        動的VMは従来通りディスク・状態ファイルも消す。
        対象がなければ False。
        """
        docker = self._docker()
        if vm_id in (BASE_VM_SERVICE, DESKTOP_SERVICE, ANDROID_SERVICE):
            targets = self._lifecycle_targets(vm_id, docker, include_relay=True)
            if not targets and self._load_state(vm_id) is None:
                return False
            for c in targets:
                try:
                    c.stop(timeout=10)
                except Exception:
                    logger.debug("コンテナ停止に失敗", exc_info=True)
                try:
                    c.remove(force=True)
                except Exception as e:
                    raise DockerUnavailableError(f"環境の削除に失敗しました: {c.name}") from e
            EnvironmentNetworks(docker, self._project_name(docker)).remove(vm_id)
            self._drop_state(vm_id)
            logger.info("VM削除: %s", vm_id)
            return True
        targets = self._dynamic_containers(docker, vm_id)
        if not targets:
            state = self._load_state(vm_id) or {}
            if not state.get("isolated_network") or docker.containers.list(
                all=True, filters={"label": f"{LABEL_VM_ID}={vm_id}"}
            ):
                return False
        for c in targets:
            try:
                c.stop(timeout=10)
            except Exception:
                logger.debug("コンテナ停止に失敗", exc_info=True)
            try:
                c.remove(force=True)
            except Exception as e:
                raise DockerUnavailableError(f"環境の削除に失敗しました: {c.name}") from e
        if Path(vm_id).name != vm_id or vm_id in {".", ".."}:
            raise ValueError("環境IDが不正です")
        EnvironmentNetworks(docker, self._project_name(docker)).remove(vm_id)
        WorkspaceVolumes(docker, self._project_name(docker)).remove(vm_id)
        # This path is mounted into the backend; host paths belong to the Docker daemon.
        try:
            (Path(self._overlays_dir) / f"{vm_id}.qcow2").unlink(missing_ok=True)
        except OSError as e:
            raise DockerUnavailableError(f"VMディスクの削除に失敗しました: {vm_id}") from e
        self._drop_state(vm_id)
        logger.info("VM削除: %s", vm_id)
        return True

    # ── 内部 ────────────────────────────────────────

    def _docker(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import docker
        except ImportError as e:
            raise DockerUnavailableError("docker パッケージが利用できません") from e
        try:
            client = docker.from_env()
            client.ping()
        except Exception as e:
            raise DockerUnavailableError("Docker ソケットに接続できません") from e
        self._client = client
        return client

    @staticmethod
    def _labels(container: Any) -> dict:
        try:
            return (container.attrs.get("Config") or {}).get("Labels") or {}
        except Exception:
            return {}

    def _from_container(
        self, container: Any, vm_id: str, managed: bool, kind: str | None = None
    ) -> VmInfo:
        import contextlib

        with contextlib.suppress(Exception):
            container.reload()
        try:
            status = str(container.status)
        except Exception:
            status = "unknown"
        health = None
        try:
            health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
        except Exception:
            health = None
        saved = self._load_state(vm_id)
        name = container.name if managed else vm_id
        if saved and saved.get("name"):
            name = saved["name"]
        if kind is None:
            kind = self._labels(container).get(LABEL_KIND, KIND_QEMU)
        vnc_port = ws_port = adb_port = 0
        return VmInfo(
            id=vm_id,
            name=name,
            status=status,
            health=health,
            vnc_port=vnc_port,
            ws_port=ws_port,
            vnc_host=container.name,
            managed=managed,
            kind=kind,
            adb_port=adb_port,
        )

    def _create_overlay(self, vm_id: str) -> str:
        """baseのフルコピーを作り、ホスト側パスを返す。

        読み取り専用テンプレートから独立したディスクを複製する。
        backing参照を使わず、テンプレートへの依存を残さない。
        """
        docker = self._docker()
        overlay_host_dir = f"{self._host_repo_dir()}/vm/overlays"
        Path(self._overlays_dir).mkdir(parents=True, exist_ok=True)
        overlay_host = str(Path(overlay_host_dir).resolve() / (vm_id + ".qcow2"))
        image = self._vm_image_ref(docker)
        logger.info("VMディスク複製: %s", overlay_host)
        docker.containers.run(
            image,
            # 注意: vmイメージのENTRYPOINTはQEMU起動のため上書きする
            entrypoint=["cp"],
            network_mode="none",
            command=(
                f"--sparse=always --reflink=auto /vm/desktop.qcow2 /vm/overlays/{vm_id}.qcow2"
            ),
            volumes={
                overlay_host_dir: {"bind": "/vm/overlays", "mode": "rw"},
                # 複製元（読取専用）
                f"{self._host_repo_dir()}/vm/desktop.qcow2": {
                    "bind": BASE_IMAGE_IN_VM,
                    "mode": "ro",
                },
            },
            remove=True,
        )
        return overlay_host

    def _target_kind(self, vm_id: str, docker: Any) -> str:
        """再起動・削除対象の種別を返す。不明時は qemu 扱い（従来動作）。"""
        if vm_id == DESKTOP_SERVICE:
            return KIND_CONTAINER
        if vm_id == BASE_VM_SERVICE:
            return KIND_QEMU
        if vm_id == ANDROID_SERVICE:
            return KIND_ANDROID
        try:
            targets = self._dynamic_containers(docker, vm_id)
        except Exception:
            return KIND_QEMU
        for c in targets:
            if LABEL_WS_FOR not in self._labels(c):
                return self._labels(c).get(LABEL_KIND, KIND_QEMU)
        return KIND_QEMU

    def _android_image_ref(self, docker: Any) -> str:
        """Android実行イメージの参照。環境変数優先、なければ既定から検出。"""
        if os.environ.get("ANDROID_IMAGE_REF"):
            return os.environ["ANDROID_IMAGE_REF"]
        try:
            candidates = self._fixed_containers(docker, ANDROID_SERVICE)
            if candidates:
                tags = (
                    (candidates[0].image.attrs.get("RepoTags") or [])
                    if hasattr(candidates[0], "image")
                    else []
                )
                image = candidates[0].attrs.get("Image", "")
                return tags[0] if tags else image
        except Exception:
            logger.debug("androidイメージ検出に失敗", exc_info=True)
        return "ai-desktop-agent-android:latest"

    def _android_iso_host(self) -> str | None:
        """ホスト側のAndroid ISOパスを返す。なければ None。

        明示指定（ANDROID_ISO_HOST）があれば優先。なければリポジトリ直下の
        `android/android-x86_64.iso` を使う。
        backendがコンテナ内で動く場合、ホストパスはDockerボリューム元として
        使い、存在確認はコンテナ内マウント（`/app/android-host`）でも行う。
        """
        explicit = os.environ.get("ANDROID_ISO_HOST")
        if explicit:
            return explicit if Path(explicit).is_file() else None
        host_path = Path(f"{self._host_repo_dir()}/android/android-x86_64.iso")
        if host_path.is_file():
            return str(host_path.resolve())
        # backendコンテナ内からはホストパスが見えないため、マウント経由で確認する
        container_path = Path("/app/android-host/android-x86_64.iso")
        if container_path.is_file():
            return str(host_path)
        return None

    def _android_ro_files(self) -> dict:
        """カーネル等の読取専用マウント。存在するもののみ返す。

        ISO直起動フォールバックのため、カーネルがなくても起動できる。
        存在しないホストパスをマウントするとDockerがディレクトリを作って
        しまうため、事前に存在確認する。backendコンテナ内からはホストパスが
        見えないため、`/app/android-host` マウント経由でも確認する。
        """
        host_dir = f"{self._host_repo_dir()}/android"
        mounts: dict = {}
        for fname, guest in (
            ("kernel", "/android/kernel"),
            ("initrd.img", "/android/initrd.img"),
        ):
            host = f"{host_dir}/{fname}"
            if Path(host).is_file() or Path(f"/app/android-host/{fname}").is_file():
                mounts[host] = {"bind": guest, "mode": "ro"}
        return mounts

    def _desktop_image_ref(self, docker: Any) -> str:
        """軽量デスクトップ実行イメージの参照。環境変数優先、なければ既定から検出。"""
        if os.environ.get("DESKTOP_IMAGE_REF"):
            return os.environ["DESKTOP_IMAGE_REF"]
        try:
            candidates = self._fixed_containers(docker, DESKTOP_SERVICE)
            if candidates:
                tags = (
                    (candidates[0].image.attrs.get("RepoTags") or [])
                    if hasattr(candidates[0], "image")
                    else []
                )
                image = candidates[0].attrs.get("Image", "")
                return tags[0] if tags else image
        except Exception:
            logger.debug("desktopイメージ検出に失敗", exc_info=True)
        return "ai-desktop-agent-desktop:latest"

    def _vm_image_ref(self, docker: Any) -> str:
        """QEMU実行イメージの参照。環境変数優先、なければ既定VMから検出。"""
        if os.environ.get("VM_IMAGE_REF"):
            return os.environ["VM_IMAGE_REF"]
        try:
            legacy = self._fixed_containers(docker, BASE_VM_SERVICE)
            if legacy:
                tags = (
                    (legacy[0].image.attrs.get("RepoTags") or [])
                    if hasattr(legacy[0], "image")
                    else []
                )
                image = legacy[0].attrs.get("Image", "")
                return tags[0] if tags else image
        except Exception:
            logger.debug("VMイメージ検出に失敗", exc_info=True)
        return "ai-desktop-agent-vm:latest"

    def _ro_vm_files(self) -> dict:
        """カーネル等の読取専用マウント。"""
        host_dir = f"{self._host_repo_dir()}/vm"
        return {
            f"{host_dir}/vmlinuz": {"bind": "/vm/vmlinuz", "mode": "ro"},
            f"{host_dir}/initrd.img": {"bind": "/vm/initrd.img", "mode": "ro"},
            f"{host_dir}/cmdline.txt": {"bind": "/vm/cmdline.txt", "mode": "ro"},
        }

    def _host_repo_dir(self) -> str:
        """ホスト側のリポジトリパスを特定する。

        backend自身の /app/data マウント元から逆算する。
        明示指定（VM_HOST_DIR）があれば優先。ホスト直実行時はカレント。
        """
        explicit = os.environ.get("VM_HOST_DIR")
        if explicit:
            return explicit.rstrip("/")
        try:
            docker = self._docker()
            me = docker.containers.get(socket.gethostname())
            for m in me.attrs.get("Mounts", []) or []:
                if m.get("Destination") == "/app/data":
                    src = m.get("Source", "")
                    if src.endswith("/data"):
                        return src[: -len("/data")]
                    if src:
                        return str(Path(src).parent)
        except Exception:
            logger.debug("ホストパスの逆算に失敗", exc_info=True)
        return "."

    def _backend_network(self, docker: Any) -> str | None:
        """backend自身が属するネットワーク名。なければ None。"""
        try:
            hostname = socket.gethostname()
            me = docker.containers.get(hostname)
            nets = (me.attrs.get("NetworkSettings") or {}).get("Networks") or {}
            preferred = os.environ.get(
                "AGENT_MANAGEMENT_NETWORK", f"{self._project_name(docker)}_default"
            )
            if preferred in nets:
                return preferred
            if nets:
                return sorted(nets)[0]
        except Exception:
            logger.debug("所属ネットワーク検出に失敗", exc_info=True)
        return os.environ.get("BACKEND_NETWORK")

    def _vnc_reachable(self, host: str, port: int = 5900, timeout: float = 3.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    # ── 状態ファイル ────────────────────────────────

    def _state_path(self) -> Path:
        return self._state_file

    def _load_all_states(self) -> dict:
        path = self._state_path()
        try:
            states = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StorageError(
                f"VM情報を読み込めません: {path}。バックアップから復元してください"
            ) from error
        if not isinstance(states, dict) or any(
            not isinstance(value, dict) for value in states.values()
        ):
            raise StorageError(f"VM情報の形式が不正です: {path}")
        return states

    def _load_state(self, vm_id: str) -> dict | None:
        return self._load_all_states().get(vm_id)

    def _save_state(self, vm_id: str, info: dict) -> None:
        path = self._state_path()
        with metadata_lock(path):
            states = self._load_all_states()
            states[vm_id] = {**states.get(vm_id, {}), **info, "updated_at": time.time()}
            atomic_json(path, states)

    def _drop_state(self, vm_id: str) -> None:
        path = self._state_path()
        with metadata_lock(path):
            states = self._load_all_states()
            states.pop(vm_id, None)
            atomic_json(path, states)

    @contextmanager
    def _creation(self, docker: Any, vm_id: str, allocation: dict):
        # Reserve durable metadata before allocating containers or disk space.
        self._save_state(vm_id, allocation)
        try:
            yield
        except Exception:
            removed = True
            try:
                targets = docker.containers.list(
                    all=True, filters={"label": f"{LABEL_VM_ID}={vm_id}"}
                )
                for container in targets:
                    if self._labels(container).get(LABEL_PROJECT) != self._project_name(docker):
                        continue
                    try:
                        container.remove(force=True)
                    except Exception:
                        removed = False
                        logger.exception("作成失敗後のコンテナ削除に失敗: %s", container.name)
                if removed:
                    EnvironmentNetworks(docker, self._project_name(docker)).remove(vm_id)
                    if allocation.get("home_volume"):
                        WorkspaceVolumes(docker, self._project_name(docker)).remove(vm_id)
                    (Path(self._overlays_dir) / f"{vm_id}.qcow2").unlink(missing_ok=True)
                    self._drop_state(vm_id)
            except Exception:
                logger.exception("作成失敗後のリソースを回収できませんでした: %s", vm_id)
            raise
