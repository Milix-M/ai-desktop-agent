"""動的VMプール — 複数VMの作成・削除・発見。

各VMは以下で構成される:
- QEMUコンテナ（`vm-<id>`）: baseのフルコピーを起動（共有しない）
- websockifyコンテナ（`ws-<id>`）: ブラウザ向けVNC中継

compose の `vm` サービス（固定ポート）は id `vm` として自動検出する。
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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ai_desktop_agent.server.vm_control import DockerUnavailableError  # noqa: E402

LABEL_VM_ID = "ai-desktop-agent.vm-id"
LABEL_WS_FOR = "ai-desktop-agent.ws-for"
LABEL_KIND = "ai-desktop-agent.kind"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

KIND_QEMU = "qemu"
KIND_CONTAINER = "container"

BASE_VM_SERVICE = "vm"
#: 軽量コンテナ実行環境（QEMU不要のVNCデスクトップ）。既定の作業環境。
DESKTOP_SERVICE = "desktop"
#: compose管理サービスの既定ポート（ホスト公開用。保存値がない場合）。
DESKTOP_VNC_PORT = 5901
DESKTOP_WS_PORT = 6081
OVERLAYS_DIR = "/vm/overlays"  # backendコンテナ内のマウント先
BASE_IMAGE_IN_VM = "/vm/desktop.qcow2"  # vmコンテナ内から見たbase
VNC_START_PORT = 5910
WS_START_PORT = 6090
MAX_VMS = 8


@dataclasses.dataclass
class VmInfo:
    """1台のVMの情報。"""

    id: str
    name: str
    status: str  # running / exited / creating / not_found
    health: str | None = None
    vnc_port: int = 5900  # ホスト側公開ポート
    ws_port: int = 6080  # ホスト側公開ポート
    vnc_host: str = ""  # backend からの接続先ホスト名
    managed: bool = True  # False: compose管理の既定VM・desktop
    kind: str = KIND_QEMU  # KIND_QEMU / KIND_CONTAINER

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class VmPool:
    """VMコンテナ群のライフサイクル管理。

    Args:
        client: テスト用の差し替え可能な Docker クライアント。
        state_file: ポート割当等の永続化先。省略時は `$DATA_DIR/vms.json`。
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

    def list_vms(self) -> list[VmInfo]:
        """実行中・停止中の全VMを返す（compose既定VM・desktopを含む）。"""
        docker = self._docker()
        found: dict[str, VmInfo] = {}

        # compose管理の既定VM（サービス名 vm。kvmプロファイル時のみ存在）
        try:
            legacy = docker.containers.list(
                all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={BASE_VM_SERVICE}"}
            )
        except Exception:
            legacy = []
        for c in legacy:
            found[BASE_VM_SERVICE] = self._from_container(
                c, BASE_VM_SERVICE, managed=False, kind=KIND_QEMU
            )

        # compose管理の軽量コンテナ環境（サービス名 desktop。常時起動が既定）
        try:
            desktops = docker.containers.list(
                all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={DESKTOP_SERVICE}"}
            )
        except Exception:
            desktops = []
        for c in desktops:
            found[DESKTOP_SERVICE] = self._from_container(
                c, DESKTOP_SERVICE, managed=False, kind=KIND_CONTAINER
            )

        # 動的VM
        try:
            dynamic = docker.containers.list(all=True, filters={"label": LABEL_VM_ID})
        except Exception:
            dynamic = []
        for c in dynamic:
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
        """タスク投入先の既定VM（稼働中の最初の1台）。なければ None。"""
        for vm in self.list_vms():
            if vm.status == "running":
                return vm
        return None

    # ── 作成・削除 ──────────────────────────────────

    def create_vm(self, name: str | None = None, kind: str = KIND_QEMU) -> VmInfo:
        """新しい環境を作成して起動する。本体 + 中継の2コンテナ。

        kind=qemu: QEMU VM（overlay + 2コンテナ）。KVM必須のため、KVMが
        利用できない環境では作成を制限する（`KvmUnavailableError`）。
        kind=container: 軽量デスクトップコンテナ。KVM不要でどこでも作れる。
        """
        from ai_desktop_agent.server.kvm import (
            KvmUnavailableError,
            is_kvm_available,
            is_tcg_allowed,
            resolve_use_kvm,
        )

        if kind not in (KIND_QEMU, KIND_CONTAINER):
            raise ValueError(f"kind は qemu/container のいずれか: {kind}")

        docker = self._docker()
        if kind == KIND_QEMU and not is_tcg_allowed() and not is_kvm_available(docker):
            raise KvmUnavailableError()
        if len([v for v in self.list_vms() if v.managed]) >= MAX_VMS:
            raise ValueError(f"VM数の上限に達しています（{MAX_VMS}）")

        prefix = "vm" if kind == KIND_QEMU else "desk"
        vm_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        vnc_port = self._alloc_port(VNC_START_PORT, lambda v: v.vnc_port)
        ws_port = self._alloc_port(WS_START_PORT, lambda v: v.ws_port)

        network = self._backend_network(docker)
        common_labels = {LABEL_VM_ID: vm_id, LABEL_KIND: kind}
        vm_name = f"ai-desktop-agent-{vm_id}"
        logger.info("VM作成: %s kind=%s (vnc=%d ws=%d)", vm_id, kind, vnc_port, ws_port)

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
                # QEMU VMはKVM必須。非対応ホストでは起動前段で弾くため常時要求する。
                devices=["/dev/kvm:/dev/kvm"],
                ports={"5900/tcp": vnc_port},
                environment={
                    "VM_MEMORY": memory,
                    "VM_CPUS": cpus,
                    "VM_VNC_PORT": "5900",
                    "VM_IMAGE": f"/vm/overlays/{vm_id}.qcow2",
                    "CMDLINE_FILE": "/vm/cmdline.txt",
                    "USE_KVM": use_kvm,
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
            docker.containers.run(
                image,
                name=vm_name,
                detach=True,
                ports={"5900/tcp": vnc_port},
                environment={
                    "VNC_GEOMETRY": os.environ.get("VNC_GEOMETRY", "1280x800"),
                    "VNC_DEPTH": os.environ.get("VNC_DEPTH", "24"),
                },
                labels=common_labels,
                network=network,
                restart_policy={"Name": "unless-stopped"},
            )

        ws_image = os.environ.get("WEBSOCKIFY_IMAGE", "ai-desktop-agent-websockify")
        docker.containers.run(
            ws_image,
            name=f"ai-desktop-agent-ws-{vm_id}",
            command=f"0.0.0.0:6080 {vm_name}:5900",
            detach=True,
            ports={"6080/tcp": ws_port},
            labels={**common_labels, LABEL_WS_FOR: vm_id},
            network=network,
            restart_policy={"Name": "unless-stopped"},
        )

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
        """VMコンテナを再起動する（ゲストOSごと作り直し）。中継は触らない。

        QEMU VMはKVM必須のため、KVMが利用できない環境では再起動を制限する
        （`KvmUnavailableError`）。コンテナ環境の再起動はどこでもできる。
        """
        from ai_desktop_agent.server.kvm import (
            KvmUnavailableError,
            is_kvm_available,
            is_tcg_allowed,
        )

        docker = self._docker()
        if (
            self._target_kind(vm_id, docker) == KIND_QEMU
            and not is_tcg_allowed()
            and not is_kvm_available(docker)
        ):
            raise KvmUnavailableError()
        if vm_id == BASE_VM_SERVICE:
            targets = docker.containers.list(
                all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={BASE_VM_SERVICE}"}
            )
        else:
            targets = [
                c
                for c in docker.containers.list(
                    all=True, filters={"label": f"{LABEL_VM_ID}={vm_id}"}
                )
                if LABEL_WS_FOR not in self._labels(c)
            ]
        if not targets:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        for c in targets:
            c.restart(timeout=timeout)
            logger.info("VM再起動: %s", c.name)
        info = self.get_vm(vm_id)
        if info is None:
            raise ValueError(f"VMが見つかりません: {vm_id}")
        return info

    def remove_vm(self, vm_id: str) -> bool:
        """VMと中継コンテナを削除し、ディスクを消す。既定VM・desktopは不可。"""
        if vm_id in (BASE_VM_SERVICE, DESKTOP_SERVICE):
            raise ValueError("既定VM・コンテナ環境は削除できません")
        docker = self._docker()
        targets = docker.containers.list(all=True, filters={"label": f"{LABEL_VM_ID}={vm_id}"})
        if not targets:
            return False
        for c in targets:
            try:
                c.stop(timeout=10)
            except Exception:
                logger.debug("コンテナ停止に失敗", exc_info=True)
            try:
                c.remove(force=True)
            except Exception:
                logger.warning("コンテナ削除に失敗", exc_info=True)
        try:
            Path(f"{self._host_repo_dir()}/vm/overlays/{vm_id}.qcow2").unlink(missing_ok=True)
        except OSError:
            logger.debug("VMディスク削除をスキップ", exc_info=True)
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
        if saved:
            vnc_port = int(saved.get("vnc_port", 5900))
            ws_port = int(saved.get("ws_port", 6080))
        elif vm_id == DESKTOP_SERVICE:
            vnc_port, ws_port = DESKTOP_VNC_PORT, DESKTOP_WS_PORT
        else:
            vnc_port, ws_port = 5900, 6080
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
        )

    def _alloc_port(self, start: int, key) -> int:
        used = {key(v) for v in self.list_vms()}
        port = start
        while port in used:
            port += 1
        return port

    def _create_overlay(self, vm_id: str) -> str:
        """baseのフルコピーを作り、ホスト側パスを返す。

        注意: qcow2 backing参照（overlay）は使わない。
        baseを別QEMUが読み書きオープン中の場合、backing参照側が
        ファイルロックで開けないため。容量と引き換えに堅牢性を取る。
        """
        docker = self._docker()
        overlay_host_dir = f"{self._host_repo_dir()}/vm/overlays"
        Path(overlay_host_dir).mkdir(parents=True, exist_ok=True)
        overlay_host = str(Path(overlay_host_dir).resolve() / (vm_id + ".qcow2"))
        image = self._vm_image_ref(docker)
        logger.info("VMディスク複製: %s", overlay_host)
        docker.containers.run(
            image,
            # 注意: vmイメージのENTRYPOINTはQEMU起動のため上書きする
            entrypoint=["cp"],
            command=(f"--sparse=always /vm/desktop.qcow2 /vm/overlays/{vm_id}.qcow2"),
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
        try:
            targets = docker.containers.list(all=True, filters={"label": f"{LABEL_VM_ID}={vm_id}"})
        except Exception:
            return KIND_QEMU
        for c in targets:
            if LABEL_WS_FOR not in self._labels(c):
                return self._labels(c).get(LABEL_KIND, KIND_QEMU)
        return KIND_QEMU

    def _desktop_image_ref(self, docker: Any) -> str:
        """軽量デスクトップ実行イメージの参照。環境変数優先、なければ既定から検出。"""
        if os.environ.get("DESKTOP_IMAGE_REF"):
            return os.environ["DESKTOP_IMAGE_REF"]
        try:
            candidates = docker.containers.list(
                all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={DESKTOP_SERVICE}"}
            )
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
            legacy = docker.containers.list(
                all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={BASE_VM_SERVICE}"}
            )
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
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _load_state(self, vm_id: str) -> dict | None:
        return self._load_all_states().get(vm_id)

    def _save_state(self, vm_id: str, info: dict) -> None:
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            all_states = self._load_all_states()
            all_states[vm_id] = {**info, "updated_at": time.time()}
            path.write_text(json.dumps(all_states, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("VM状態ファイルの保存に失敗", exc_info=True)

    def _drop_state(self, vm_id: str) -> None:
        try:
            all_states = self._load_all_states()
            all_states.pop(vm_id, None)
            self._state_path().write_text(
                json.dumps(all_states, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
