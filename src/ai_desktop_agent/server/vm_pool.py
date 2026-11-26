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
import math
import os
import re
import shutil
import socket
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ai_desktop_agent.server.desktop_resources import (  # noqa: E402
    DesktopResources,
    desktop_runtime_from_env,
)
from ai_desktop_agent.server.docker_client import DockerClientOwner, docker_operation  # noqa: E402
from ai_desktop_agent.server.docker_discovery import (  # noqa: E402
    container_labels,
    refresh_existing,
)
from ai_desktop_agent.server.networks import (  # noqa: E402
    LABEL_PROJECT,
    LABEL_VM_ID,
    EnvironmentNetworks,
)
from ai_desktop_agent.server.persistence import (  # noqa: E402
    StorageError,
    atomic_json,
    metadata_lock,
    unique_json_object,
)
from ai_desktop_agent.server.readiness import is_ready  # noqa: E402
from ai_desktop_agent.server.vm_control import DockerUnavailableError  # noqa: E402
from ai_desktop_agent.server.vm_locks import (  # noqa: E402
    environment_lock,
    serialize_creation,
    serialize_environment,
)
from ai_desktop_agent.server.workspaces import HOME, WorkspaceVolumes, identifier  # noqa: E402
from ai_desktop_agent.validation import validate_text  # noqa: E402

LABEL_SCHEDULE_RECEIPT = "ai-desktop-agent.schedule-creation"
LABEL_WS_FOR = "ai-desktop-agent.ws-for"
LABEL_DISK_COPY = "ai-desktop-agent.disk-copy-for"
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


class _MissingResourceMountError(DockerUnavailableError):
    """No bind was configured; optional Android media can be genuinely absent."""


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


@dataclasses.dataclass(frozen=True)
class VmInstance:
    info: VmInfo
    container_id: str


class VmPool(DockerClientOwner):
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
        super().__init__(client)
        base = os.environ.get("DATA_DIR", "./data")
        self._state_file = Path(state_file or Path(base) / "vms.json")
        self._overlays_dir = overlays_dir

    # ── 一覧・取得 ──────────────────────────────────

    @docker_operation
    def instance_id(self, vm_id: str) -> str | None:
        """Identify the current primary container, including replacement under the same VM ID."""
        target = self._primary_container(vm_id, self._docker())
        return target.id if target is not None else None

    @docker_operation
    @serialize_environment
    def get_instance(self, vm_id: str) -> VmInstance | None:
        """Bind the connection information and physical ID to one inspected container."""
        target = self._primary_container(vm_id, self._docker())
        if target is None:
            return None
        kind = {
            BASE_VM_SERVICE: KIND_QEMU,
            DESKTOP_SERVICE: KIND_CONTAINER,
            ANDROID_SERVICE: KIND_ANDROID,
        }.get(vm_id)
        info = self._from_container(target, vm_id, managed=kind is None, kind=kind)
        if info is None:
            return None
        if not isinstance(target.id, str) or not target.id:
            raise DockerUnavailableError("環境の識別子を確認できません")
        return VmInstance(info=info, container_id=target.id)

    @docker_operation
    def isolate_environments(self) -> None:
        # Snapshot discovery before detaching old shared networks used for legacy adoption.
        environments = self.list_vms()
        for vm in environments:
            self.prepare_vm(vm.id)

    @docker_operation
    @serialize_environment
    def prepare_vm(self, vm_id: str) -> None:
        identifier(vm_id)
        state = self._load_state(vm_id) or {}
        docker = self._docker()
        networks = EnvironmentNetworks(docker, self._project_name(docker))
        target = self._primary_container(vm_id, docker)
        if target is None:
            return
        network = networks.ensure(vm_id, target)
        if state.get("isolated_network") != network.name:
            # Keep legacy environments discoverable even if a later migration step fails.
            self._save_state(vm_id, {**state, "isolated_network": network.name})
        networks.isolate(vm_id, target)

    def _primary_container(self, vm_id: str, docker: Any):
        targets = self._lifecycle_targets(vm_id, docker, include_relay=False)
        if vm_id in (BASE_VM_SERVICE, DESKTOP_SERVICE, ANDROID_SERVICE):
            return self._pick_fixed(targets, vm_id)
        if len(targets) > 1:
            raise DockerUnavailableError(f"同じ環境IDの本体が複数あります: {vm_id}")
        return targets[0] if targets else None

    @docker_operation
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
            all=True,
            filters={
                "label": [
                    f"{COMPOSE_SERVICE_LABEL}={service}",
                    f"com.docker.compose.project={project}",
                ]
            },
            sparse=True,
        )
        owned = (
            c for c in candidates if self._labels(c).get("com.docker.compose.project") == project
        )
        return [
            c
            for c in refresh_existing(owned)
            if self._labels(c).get("com.docker.compose.project") == project
        ]

    @_discovery_operation
    def _dynamic_containers(self, docker: Any, vm_id: str | None = None) -> list[Any]:
        label = LABEL_VM_ID if vm_id is None else f"{LABEL_VM_ID}={vm_id}"
        candidates = docker.containers.list(all=True, filters={"label": label}, sparse=True)
        project = self._project_name(docker)
        states = self._load_all_states()
        network = self._backend_network(docker)

        def owned(container):
            labels = self._labels(container)
            owner = labels.get(LABEL_PROJECT)
            if owner is not None:
                return owner == project
            # Legacy containers have no owner label. Require this store's allocation
            # AND this backend's network; a shared Docker daemon alone proves nothing.
            nets = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
            if labels.get(LABEL_VM_ID) in states and network and network in nets:
                return True
            vm_key = labels.get(LABEL_VM_ID, "")
            isolated = (states.get(vm_key) or {}).get("isolated_network")
            manager = EnvironmentNetworks(docker, project)
            return bool(
                isolated
                and isolated in nets
                and isolated == manager.name_for(vm_key)
                and manager.owned(docker.networks.get(isolated), vm_key)
            )

        # Network membership is mutable: verify legacy ownership again after inspection.
        candidates = (container for container in candidates if owned(container))
        return [container for container in refresh_existing(candidates) if owned(container)]

    @staticmethod
    def _pick_fixed(candidates: list, service: str) -> Any | None:
        """固定サービスのコンテナを1つ選ぶ。

        同一ラベルを持つ手動コンテナが混ざると影武者になる
        （例: android-nowiz が android を乗っ取り、VNC接続先が到達不能に）。
        Compose命名（<project>-<service>-Nと旧形式の区切り_）を優先し、
        なければ先頭を返す。
        """
        if not candidates:
            return None
        for c in candidates:
            project = VmPool._labels(c).get("com.docker.compose.project", "ai-desktop-agent")
            name = getattr(c, "name", "")
            for separator in ("-", "_"):
                prefix = f"{project}{separator}{service}{separator}"
                if (
                    name.startswith(prefix)
                    and name[len(prefix) :].isascii()
                    and name[len(prefix) :].isdigit()
                ):
                    return c
        return candidates[0]

    @docker_operation
    def list_vms(self) -> list[VmInfo]:
        """実行中・停止中の全VMを返す（compose既定VM・desktopを含む）。"""
        docker = self._docker()
        found: dict[str, VmInfo] = {}

        for service, kind in (
            (BASE_VM_SERVICE, KIND_QEMU),
            (DESKTOP_SERVICE, KIND_CONTAINER),
            (ANDROID_SERVICE, KIND_ANDROID),
        ):
            picked = self._primary_container(service, docker)
            if picked is not None:
                info = self._from_container(picked, service, managed=False, kind=kind)
                if info is not None:
                    found[service] = info

        # 動的VM
        dynamic = self._dynamic_containers(docker)
        for c in dynamic:
            if LABEL_WS_FOR in self._labels(c) or LABEL_DISK_COPY in self._labels(c):
                continue
            vm_id = self._labels(c).get(LABEL_VM_ID, "")
            if vm_id and vm_id != BASE_VM_SERVICE:
                info = self._from_container(c, vm_id, managed=True)
                if info is not None:
                    if vm_id in found:
                        raise DockerUnavailableError(f"同じ環境IDの本体が複数あります: {vm_id}")
                    found[vm_id] = info

        return [found[k] for k in sorted(found)]

    @docker_operation
    def get_vm(self, vm_id: str) -> VmInfo | None:
        """1台取得。なければ None。"""
        for vm in self.list_vms():
            if vm.id == vm_id:
                return vm
        return None

    @docker_operation
    def default_vm(self) -> VmInfo | None:
        """タスク投入先の既定VM（稼働中の最初の1台）。なければ None。

        compose管理の `desktop` が稼働していれば優先する。
        """
        running = [vm for vm in self.list_vms() if is_ready(vm.status, vm.health)]
        for vm in running:
            if vm.id == DESKTOP_SERVICE:
                return vm
        return running[0] if running else None

    # ── 作成・削除 ──────────────────────────────────

    @docker_operation
    @serialize_creation
    def create_vm(
        self,
        name: str | None = None,
        kind: str = KIND_QEMU,
        *,
        vm_id: str | None = None,
        schedule_receipt: str | None = None,
    ) -> VmInfo:
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

        if name is not None:
            validate_text(name, "環境名")
            if len(name) > 128:
                raise ValueError("環境名は128文字以内で指定してください")
        if kind not in (KIND_QEMU, KIND_CONTAINER, KIND_ANDROID):
            raise ValueError(f"kind は qemu/container/android のいずれか: {kind}")

        resources = DesktopResources.from_env() if kind == KIND_CONTAINER else None
        runtime = desktop_runtime_from_env() if kind == KIND_CONTAINER else None
        docker = self._docker()
        if runtime == "runsc":
            self._require_runsc(docker)
        if (
            kind in (KIND_QEMU, KIND_ANDROID)
            and not is_tcg_allowed()
            and not is_kvm_available(docker)
        ):
            raise KvmUnavailableError()
        listed = self.list_vms()
        if len([v for v in listed if v.managed]) >= MAX_VMS:
            raise ValueError(f"VM数の上限に達しています（{MAX_VMS}）")

        prefix = ID_PREFIX[kind]
        if vm_id is None:
            vm_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        elif not re.fullmatch(rf"{prefix}-[0-9a-f]{{24}}", vm_id):
            raise ValueError("作成する環境のIDが不正です")
        elif any(vm.id == vm_id for vm in listed) or self._load_state(vm_id) is not None:
            raise ValueError("同じIDの環境が既に存在します")
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
        if schedule_receipt is not None:
            common_labels[LABEL_SCHEDULE_RECEIPT] = schedule_receipt
            allocation["schedule_receipt"] = schedule_receipt
        if kind == KIND_CONTAINER:
            allocation["home_volume"] = WorkspaceVolumes(
                docker, self._project_name(docker)
            ).name_for(vm_id)
        with environment_lock(vm_id), self._creation(docker, vm_id, allocation):
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
                image = self._vm_image_ref(docker)
                overlay_host = self._create_overlay(vm_id, image=image)
                # 未設定時は entrypoint 側で /dev/kvm の有無から自動切替する
                use_kvm = resolve_use_kvm()
                memory = os.environ.get("VM_MEMORY", "4096")
                cpus = os.environ.get("VM_CPUS", "4")

                volumes = {
                    str(Path(overlay_host).parent): {"bind": "/vm/instance", "mode": "rw"},
                    self._host_path("vm/sockets", "/vm/sockets", shared=True): {
                        "bind": "/vm/sockets",
                        "mode": "rw",
                    },
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
                        "VM_IMAGE": "/vm/instance/disk.qcow2",
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
                    runtime=runtime,
                    cap_drop=["NET_RAW"],
                    shm_size="1g",
                    **resources.docker_options(),
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

    @docker_operation
    @serialize_environment
    def restart_vm(self, vm_id: str, timeout: int = 30) -> VmInfo:
        """VMコンテナを再起動する（ゲストOSを含む再起動）。中継は触らない。

        QEMU系（VM・Android）はKVM必須のため、KVMが利用できない環境では
        再起動を制限する（`KvmUnavailableError`）。
        コンテナ環境の再起動はどこでもできる。
        """
        identifier(vm_id)
        self._load_all_states()
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
            self._target_kind(vm_id, targets) in (KIND_QEMU, KIND_ANDROID)
            and not is_tcg_allowed()
            and not is_kvm_available(docker)
        ):
            raise KvmUnavailableError()
        for c in targets:
            try:
                c.restart(timeout=timeout)
            except Exception as error:
                raise DockerUnavailableError(
                    f"環境の再起動を確認できません。状態を確認してから操作してください: {c.name}"
                ) from error
            logger.info("VM再起動: %s", c.name)
        info = self.get_vm(vm_id)
        if info is None:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        return info

    @docker_operation
    @serialize_environment
    def stop_vm(self, vm_id: str, timeout: int = 10) -> VmInfo:
        """VM/コンテナを停止する（固定・動的とも可）。中継も連動して止める。

        停止済みの場合は何もせず現在情報を返す。存在しなければ ValueError。
        """
        identifier(vm_id)
        self._load_all_states()
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

    @docker_operation
    @serialize_environment
    def start_vm(self, vm_id: str) -> VmInfo:
        """VM/コンテナを起動する（固定・動的とも可）。中継も連動して起動する。

        QEMU系（VM・Android）はKVM必須のため、KVMが利用できない環境では
        起動を制限する（`KvmUnavailableError`）。
        存在しなければ ValueError。
        """
        identifier(vm_id)
        self._load_all_states()
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
            self._target_kind(vm_id, targets) in (KIND_QEMU, KIND_ANDROID)
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
        all_targets = [
            c
            for c in self._dynamic_containers(docker, vm_id)
            if LABEL_DISK_COPY not in self._labels(c)
        ]
        if include_relay:
            return list(all_targets)
        return [c for c in all_targets if LABEL_WS_FOR not in self._labels(c)]

    @docker_operation
    @serialize_environment
    def get_scheduled_instance(self, vm_id: str, receipt: str) -> VmInstance | None:
        target = self._primary_container(vm_id, self._docker())
        if target is None or self._labels(target).get(LABEL_SCHEDULE_RECEIPT) != receipt:
            return None
        info = self._from_container(target, vm_id, managed=True)
        if info is None or not isinstance(target.id, str) or not target.id:
            raise DockerUnavailableError("予約した環境の識別子を確認できません")
        return VmInstance(info, target.id)

    @docker_operation
    @serialize_environment
    def remove_scheduled_vm(self, vm_id: str, instance_id: str | None, receipt: str):
        # A failed creation may leave only volumes or disk state. Its receipt must match
        # even when there is no longer a container to inspect.
        state = self._load_state(vm_id)
        if not state or state.get("schedule_receipt") != receipt:
            return
        targets = self._dynamic_containers(self._docker(), vm_id)
        if any(
            self._labels(c).get(LABEL_SCHEDULE_RECEIPT) != receipt
            or (instance_id and c.id != instance_id)
            for c in targets
        ):
            return
        if not self.remove_vm(vm_id):
            raise DockerUnavailableError("予約した環境の削除を確認できません")

    @docker_operation
    @serialize_environment
    def remove_vm(self, vm_id: str) -> bool:
        """VM/コンテナを削除する（固定・動的とも可）。

        固定サービス（vm/desktop/android）は本体＋中継コンテナを停止・削除する
        （`docker compose up` で復元できる。ディスクイメージは消さない）。
        注意: 固定androidと同一composeラベルを持つ手動コンテナ
        （`android-nowiz` 等）も対象になる。
        動的VMは従来通りディスク・状態ファイルも消す。
        対象がなければ False。
        """
        identifier(vm_id)
        self._load_all_states()
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
                all=True, filters={"label": f"{LABEL_VM_ID}={vm_id}"}, sparse=True
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
        EnvironmentNetworks(docker, self._project_name(docker)).remove(vm_id)
        WorkspaceVolumes(docker, self._project_name(docker)).remove(vm_id)
        # This path is mounted into the backend; host paths belong to the Docker daemon.
        try:
            self._remove_disk(vm_id)
        except OSError as e:
            raise DockerUnavailableError(f"VMディスクの削除に失敗しました: {vm_id}") from e
        self._drop_state(vm_id)
        logger.info("VM削除: %s", vm_id)
        return True

    # ── 内部 ────────────────────────────────────────

    @staticmethod
    @_discovery_operation
    def _require_runsc(client: Any) -> None:
        if "runsc" not in (client.info().get("Runtimes") or {}):
            raise DockerUnavailableError(
                "DESKTOP_RUNTIME=runsc を使うにはDockerにrunscを登録してください"
            )

    @staticmethod
    def _labels(container: Any) -> dict:
        return container_labels(container)

    def _from_container(
        self, container: Any, vm_id: str, managed: bool, kind: str | None = None
    ) -> VmInfo | None:
        from docker.errors import NotFound

        try:
            container.reload()
            status = str(container.status)
            health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
        except NotFound:
            return None
        except Exception as error:
            raise DockerUnavailableError(
                "環境の状態を確認できません。Dockerの接続と権限を確認してください"
            ) from error
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

    def _create_overlay(self, vm_id: str, *, image: str | None = None) -> str:
        """baseのフルコピーを作り、ホスト側パスを返す。

        読み取り専用テンプレートから独立したディスクを複製する。
        backing参照を使わず、テンプレートへの依存を残さない。
        """
        docker = self._docker()
        overlay_host_dir = self._host_path("vm/overlays", self._overlays_dir, shared=True)
        base_host = self._host_path("vm/desktop.qcow2", BASE_IMAGE_IN_VM)
        directory = self._overlay_directory()
        directory.mkdir(parents=True, exist_ok=True)
        instance_dir = directory / vm_id
        instance_dir.mkdir(mode=0o700)
        overlay_host = str(Path(overlay_host_dir) / vm_id / "disk.qcow2")
        if image is None:
            image = self._vm_image_ref(docker)
        logger.info("VMディスク複製: %s", overlay_host)
        try:
            docker.containers.run(
                image,
                entrypoint=["bash", "-ceu"],
                network_mode="none",
                command=[
                    "if [ ! -r /prepare-boot.sh ] || [ ! -s /prepare-boot.sh ]; then exit 78; fi; "
                    "exec timeout --kill-after=5 300 cp --sparse=always --reflink=auto "
                    "/vm/desktop.qcow2 /vm/instance/disk.qcow2"
                ],
                volumes={
                    str(Path(overlay_host).parent): {"bind": "/vm/instance", "mode": "rw"},
                    base_host: {"bind": BASE_IMAGE_IN_VM, "mode": "ro"},
                },
                labels={
                    LABEL_VM_ID: vm_id,
                    LABEL_PROJECT: self._project_name(docker),
                    LABEL_DISK_COPY: vm_id,
                },
                cap_drop=["ALL"],
                user=f"{os.getuid()}:{os.getgid()}",
                remove=True,
            )
        except Exception as error:
            from docker.errors import ContainerError

            if isinstance(error, ContainerError) and error.exit_status == 78:
                raise DockerUnavailableError(
                    "VM実行イメージの更新が必要です。VMイメージを再ビルドしてください"
                ) from error
            raise DockerUnavailableError(
                "VMディスクを作成できません。テンプレートと保存先の状態を確認してください"
            ) from error
        return overlay_host

    def _target_kind(self, vm_id: str, targets: list[Any]) -> str:
        """Check the inspected targets before issuing a start or restart command."""
        kind = {
            DESKTOP_SERVICE: KIND_CONTAINER,
            BASE_VM_SERVICE: KIND_QEMU,
            ANDROID_SERVICE: KIND_ANDROID,
        }.get(vm_id)
        if kind is not None:
            mains = [c for c in targets if self._labels(c).get(COMPOSE_SERVICE_LABEL) == vm_id]
        else:
            mains = [c for c in targets if LABEL_WS_FOR not in self._labels(c)]
            if len(mains) > 1:
                raise DockerUnavailableError(f"同じ環境IDの本体が複数あります: {vm_id}")
        if not mains:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        if kind is None:
            # Legacy dynamic guests without a kind label are QEMU environments.
            kind = self._labels(mains[0]).get(LABEL_KIND, KIND_QEMU)
        if kind not in ID_PREFIX:
            raise DockerUnavailableError(f"環境の種別を確認できません: {vm_id}")
        return kind

    def _runtime_image_ref(self, docker: Any, service: str, variable: str, fallback: str) -> str:
        explicit = os.environ.get(variable)
        if explicit:
            return explicit
        source = self._pick_fixed(self._fixed_containers(docker, service), service)
        if source is None:
            return fallback
        # Tags can move between discovery and creation. Match the visible primary
        # and use its immutable image ID without another image-inspection request.
        image = source.attrs.get("Image")
        if not isinstance(image, str) or not image.strip():
            raise DockerUnavailableError(
                "既定環境の実行イメージを確認できません。環境の状態を確認してください"
            )
        return image

    def _android_image_ref(self, docker: Any) -> str:
        """Android実行イメージの参照。環境変数優先、なければ既定から検出。"""
        return self._runtime_image_ref(
            docker, ANDROID_SERVICE, "ANDROID_IMAGE_REF", "ai-desktop-agent-android:latest"
        )

    def _android_iso_host(self) -> str | None:
        """ホスト側のAndroid ISOパスを返す。なければ None。

        明示指定（ANDROID_ISO_HOST）があれば優先。なければリポジトリ直下の
        `android/android-x86_64.iso` を使う。
        backendがコンテナ内で動く場合、ホストパスはDockerボリューム元として
        使い、存在確認はコンテナ内マウント（`/app/android-host`）でも行う。
        """
        explicit = os.environ.get("ANDROID_ISO_HOST")
        if explicit:
            host_path = Path(explicit)
            if not host_path.is_absolute():
                raise DockerUnavailableError(
                    "ANDROID_ISO_HOSTにはホスト側の絶対パスを指定してください"
                )
            if host_path.is_file():
                return str(host_path)
            container_path = Path("/app/android-host") / host_path.name
            if container_path.is_file() and self._host_path(
                f"android/{host_path.name}", str(container_path), shared=True
            ) == str(host_path):
                return str(host_path)
            return None
        container_path = Path("/app/android-host/android-x86_64.iso")
        try:
            host_path = Path(
                self._host_path("android/android-x86_64.iso", str(container_path), shared=True)
            )
        except _MissingResourceMountError:
            if not container_path.is_file():
                return None
            raise
        if host_path.is_file():
            return str(host_path)
        # backendコンテナ内からはホストパスが見えないため、マウント経由で確認する
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
        try:
            host_dir = self._host_path("android", "/app/android-host", shared=True)
        except _MissingResourceMountError:
            return {}
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
        return self._runtime_image_ref(
            docker, DESKTOP_SERVICE, "DESKTOP_IMAGE_REF", "ai-desktop-agent-desktop:latest"
        )

    def _vm_image_ref(self, docker: Any) -> str:
        """QEMU実行イメージの参照。環境変数優先、なければ既定VMから検出。"""
        return self._runtime_image_ref(
            docker, BASE_VM_SERVICE, "VM_IMAGE_REF", "ai-desktop-agent-vm:latest"
        )

    def _remove_disk(self, vm_id: str) -> None:
        """Remove this VM's files through the backend mount, including legacy disks."""
        if not vm_id or Path(vm_id).name != vm_id or vm_id in {".", ".."}:
            raise ValueError("環境IDが不正です")
        directory = self._overlay_directory() / vm_id
        if directory.exists() or directory.is_symlink():
            shutil.rmtree(directory)
        (self._overlay_directory() / f"{vm_id}.qcow2").unlink(missing_ok=True)

    def _backend_mounts(self) -> list[dict] | None:
        """None means a host process; inspection failures must not become guessed paths."""
        from docker.errors import NotFound

        try:
            backend = self._docker().containers.get(socket.gethostname())
        except NotFound:
            return None
        except Exception as error:
            raise DockerUnavailableError(
                "保存先を確認できません。Dockerの状態を確認してください"
            ) from error
        return backend.attrs.get("Mounts") or []

    def _overlay_directory(self) -> Path:
        if self._overlays_dir == OVERLAYS_DIR and self._backend_mounts() is None:
            return Path(self._host_repo_dir()) / "vm/overlays"
        return Path(self._overlays_dir)

    def _host_path(self, relative: str, destination: str, *, shared: bool = False) -> str:
        """Translate a backend-visible resource into the daemon's absolute bind source."""
        if os.environ.get("VM_HOST_DIR"):
            return str(Path(self._host_repo_dir()) / relative)
        mounts = self._backend_mounts()
        if mounts is None:
            if relative == "vm/overlays" and self._overlays_dir != OVERLAYS_DIR:
                return str(Path(self._overlays_dir).resolve())
            return str(Path.cwd() / relative)
        target = Path(destination)
        candidates = [m for m in mounts if target.is_relative_to(m.get("Destination", "\0"))]
        if candidates:
            selected = max(candidates, key=lambda m: len(Path(m["Destination"]).parts))
            source = Path(selected.get("Source", ""))
            if selected.get("Type") != "bind" or not source.is_absolute():
                raise DockerUnavailableError(
                    "VM・Androidの保存先には絶対パスのbindマウントを指定してください"
                )
            return str(source / target.relative_to(selected["Destination"]))
        if shared:
            raise _MissingResourceMountError("VM・Androidの保存先マウントを確認できません")
        return str(Path(self._host_repo_dir()) / relative)

    def _host_repo_dir(self) -> str:
        """ホスト側のリポジトリパスを特定する。

        明示指定を優先し、標準配置のbindマウントからのみ逆算する。
        個別のリソースでは _host_path を使い、実際のマウント元を優先する。
        """
        explicit = os.environ.get("VM_HOST_DIR")
        if explicit:
            if not Path(explicit).is_absolute():
                raise DockerUnavailableError("VM_HOST_DIRにはホスト側の絶対パスを指定してください")
            return str(Path(explicit))
        mounts = self._backend_mounts()
        if mounts is None:
            return str(Path.cwd())
        for destination, suffix in (
            ("/vm", "vm"),
            ("/vm/overlays", "vm/overlays"),
            ("/vm/sockets", "vm/sockets"),
            ("/app/android-host", "android"),
            ("/app/data", "data"),
            ("/app", ""),
        ):
            for mount in mounts:
                source = Path(mount.get("Source", ""))
                if (
                    mount.get("Destination") == destination
                    and mount.get("Type") == "bind"
                    and source.is_absolute()
                    and (
                        not suffix or source.parts[-len(Path(suffix).parts) :] == Path(suffix).parts
                    )
                ):
                    return str(source.parents[len(Path(suffix).parts) - 1] if suffix else source)
        raise DockerUnavailableError(
            "ホスト側のVM保存先を確認できません。マウント設定を確認してください"
        )

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

    # ── 状態ファイル ────────────────────────────────

    def _state_path(self) -> Path:
        return self._state_file

    def _load_all_states(self) -> dict:
        path = self._state_path()
        try:
            states = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object
            )
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as error:
            raise StorageError(
                f"VM情報を読み込めません: {path}。バックアップから復元してください"
            ) from error
        self._validate_states(states)
        return states

    def _validate_states(self, states: dict) -> None:
        try:
            if not isinstance(states, dict):
                raise ValueError("環境情報はオブジェクトで指定してください")
            for vm_id, state in states.items():
                identifier(vm_id)
                if not isinstance(state, dict):
                    raise ValueError("環境ごとの情報はオブジェクトで指定してください")
                if "name" in state:
                    validate_text(state["name"], "環境名")
                    if len(state["name"]) > 128:
                        raise ValueError("環境名が長すぎます")
                for key in ("vnc_port", "ws_port", "adb_port"):
                    if key in state and (
                        type(state[key]) is not int or not 0 <= state[key] <= 65535
                    ):
                        raise ValueError("保存されたポートが不正です")
                if "updated_at" in state:
                    timestamp = state["updated_at"]
                    if (
                        type(timestamp) not in (int, float)
                        or not math.isfinite(timestamp)
                        or timestamp < 0
                    ):
                        raise ValueError("保存時刻が不正です")
                for key in ("isolated_network", "home_volume"):
                    if key in state:
                        validate_text(state[key], "保存先")
                        if not state[key]:
                            raise ValueError("保存先が空です")
        except (ValueError, TypeError, OverflowError) as error:
            raise StorageError(f"VM情報の形式が不正です: {self._state_path()}") from error

    def _load_state(self, vm_id: str) -> dict | None:
        return self._load_all_states().get(vm_id)

    def _save_state(self, vm_id: str, info: dict) -> None:
        path = self._state_path()
        with metadata_lock(path):
            states = self._load_all_states()
            states[vm_id] = {**states.get(vm_id, {}), **info, "updated_at": time.time()}
            self._validate_states(states)
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
        except Exception as failure:
            removed = True
            try:
                project = self._project_name(docker)
                targets = docker.containers.list(
                    all=True,
                    filters={
                        "label": [
                            f"{LABEL_VM_ID}={vm_id}",
                            f"{LABEL_PROJECT}={project}",
                        ]
                    },
                    sparse=True,
                )
                owned = (c for c in targets if self._labels(c).get(LABEL_PROJECT) == project)
                for container in refresh_existing(owned):
                    if self._labels(container).get(LABEL_PROJECT) != project:
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
                    self._remove_disk(vm_id)
                    self._drop_state(vm_id)
            except Exception:
                logger.exception("作成失敗後のリソースを回収できませんでした: %s", vm_id)
            from docker.errors import DockerException

            if isinstance(failure, (DockerException, OSError)):
                raise DockerUnavailableError(
                    "作業環境を作成できません。Dockerと保存先の状態を確認してください"
                ) from failure
            raise
