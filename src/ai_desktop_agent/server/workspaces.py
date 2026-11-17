"""Owned desktop home volumes and verified, offline filesystem backups."""

import contextlib
import hashlib
import io
import json
import os
import re
import socket
import tarfile
import tempfile
import time
import uuid
from functools import wraps
from pathlib import Path, PurePosixPath

from docker.errors import NotFound
from docker.types import Mount

from ai_desktop_agent.server.networks import LABEL_PROJECT, LABEL_VM_ID
from ai_desktop_agent.server.persistence import StorageError, atomic_json
from ai_desktop_agent.server.vm_control import DockerUnavailableError

HOME = "/home/agent"
LABEL_HOME = "ai-desktop-agent.workspace"
LABEL_SEED = "ai-desktop-agent.seed-source"
LABEL_HELPER = "ai-desktop-agent.workspace-helper"


def workspace_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (DockerUnavailableError, StorageError, ValueError):
            raise
        except Exception as error:
            raise DockerUnavailableError(
                "ホーム保存先を操作できません。Dockerの状態と権限を確認してください"
            ) from error

    return guarded


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("保存先の識別子が不正です")
    return value


class WorkspaceVolumes:
    def __init__(self, client, project: str, image: str | None = None):
        self.client = client
        self.project = identifier(project)
        self.image = image

    def name_for(self, vm_id: str) -> str:
        return f"{self.project}_home_{identifier(vm_id)}"

    def owned(self, volume, vm_id: str) -> bool:
        labels = volume.attrs.get("Labels") or {}
        return (
            (labels.get(LABEL_PROJECT) or labels.get("com.docker.compose.project")) == self.project
            and labels.get(LABEL_VM_ID) == vm_id
            and labels.get(LABEL_HOME) == "home"
            and volume.attrs.get("Driver") == "local"
            and not volume.attrs.get("Options")
        )

    @workspace_operation
    def ensure(self, vm_id: str, seed_source: str | None = None):
        name = self.name_for(vm_id)
        try:
            volume = self.client.volumes.get(name)
        except NotFound:
            labels = {LABEL_PROJECT: self.project, LABEL_VM_ID: vm_id, LABEL_HOME: "home"}
            if vm_id == "desktop":
                labels.update(
                    {
                        "com.docker.compose.project": self.project,
                        "com.docker.compose.volume": "home_desktop",
                    }
                )
            if seed_source is not None:
                labels[LABEL_SEED] = seed_source
            volume = self.client.volumes.create(name=name, driver="local", labels=labels)
        if not self.owned(volume, vm_id):
            raise DockerUnavailableError("ホーム保存先の所属を確認できません")
        if (
            seed_source is not None
            and (volume.attrs.get("Labels") or {}).get(LABEL_SEED) != seed_source
        ):
            raise DockerUnavailableError(
                "既存のホーム保存先を上書きできません。バックアップを確認してください"
            )
        return volume

    @workspace_operation
    def mounted(self, container, vm_id: str) -> bool:
        expected = self.name_for(vm_id)
        for mount in container.attrs.get("Mounts") or []:
            if (
                mount.get("Destination") == HOME
                and mount.get("Type") == "volume"
                and mount.get("Name") == expected
            ):
                volume = self.client.volumes.get(expected)
                if not self.owned(volume, vm_id):
                    raise DockerUnavailableError("ホーム保存先の所属を確認できません")
                return True
        return False

    @workspace_operation
    def require_unused(self, volume):
        if self.client.containers.list(all=True, filters={"volume": volume.name}):
            raise DockerUnavailableError(
                "ホーム保存先は使用中です。対象コンテナを削除してから再試行してください"
            )

    @workspace_operation
    def remove(self, vm_id: str):
        try:
            volume = self.client.volumes.get(self.name_for(vm_id))
        except NotFound:
            return
        if not self.owned(volume, vm_id):
            raise DockerUnavailableError("削除対象のホーム保存先の所属が一致しません")
        try:
            volume.remove(force=False)
        except Exception as error:
            raise DockerUnavailableError(
                "ホーム保存先を削除できません。使用中のコンテナを確認してください"
            ) from error

    @workspace_operation
    def reap_helpers(self):
        for container in self.client.containers.list(
            all=True, filters={"label": f"{LABEL_HELPER}=true"}
        ):
            labels = (container.attrs.get("Config") or {}).get("Labels") or {}
            if labels.get(LABEL_PROJECT) != self.project:
                continue
            mounts = container.attrs.get("Mounts") or []
            host = container.attrs.get("HostConfig") or {}
            if (
                len(mounts) != 1
                or mounts[0].get("Type") != "volume"
                or mounts[0].get("Destination") != HOME
                or host.get("NetworkMode") != "none"
                or not host.get("ReadonlyRootfs")
            ):
                continue
            volume = self.client.volumes.get(mounts[0]["Name"])
            vm_id = (volume.attrs.get("Labels") or {}).get(LABEL_VM_ID, "")
            if not self.owned(volume, vm_id) or volume.name != self.name_for(vm_id):
                continue
            owner = labels.get("ai-desktop-agent.helper-owner")
            if not owner:
                continue
            try:
                parent = self.client.containers.get(owner)
                if parent.status in {"running", "paused", "restarting"}:
                    continue
            except NotFound:
                pass
            container.remove(force=True)

    @contextlib.contextmanager
    def helper(self, volume, *, readonly=False):
        # The archive is never extracted onto the backend or the Docker host filesystem.
        helper = self.client.containers.run(
            self.image,
            entrypoint=["sleep"],
            command=["infinity"],
            detach=True,
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            cap_add=["DAC_OVERRIDE", "FOWNER", "CHOWN"],
            security_opt=["no-new-privileges:true"],
            pids_limit=16,
            mem_limit="128m",
            mounts=[
                Mount(
                    target=HOME, source=volume.name, type="volume", read_only=readonly, no_copy=True
                )
            ],
            labels={
                LABEL_PROJECT: self.project,
                LABEL_HELPER: "true",
                "ai-desktop-agent.helper-owner": socket.gethostname(),
            },
        )
        try:
            yield helper
        finally:
            helper.remove(force=True)


class _Chunks(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = memoryview(b"")

    def readable(self):
        return True

    def readinto(self, output):
        while not self.pending:
            try:
                self.pending = memoryview(next(self.chunks))
            except StopIteration:
                return 0
        count = min(len(output), len(self.pending))
        output[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count

    def close(self):
        close = getattr(self.chunks, "close", None)
        if callable(close):
            close()
        super().close()


class HomeBackups:
    def __init__(
        self,
        root: Path,
        project: str,
        *,
        max_bytes: int | None = None,
        max_files: int | None = None,
    ):
        self.project = identifier(project)
        self.root = root / "backups" / project
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else int(os.environ.get("WORKSPACE_BACKUP_MAX_BYTES", 20 * 1024**3))
        )
        self.max_files = (
            max_files
            if max_files is not None
            else int(os.environ.get("WORKSPACE_BACKUP_MAX_FILES", 200_000))
        )
        if self.max_bytes <= 0 or self.max_files <= 0:
            raise ValueError("バックアップの容量とファイル数の上限は正数にしてください")

    def capture(self, container, vm_id: str) -> str:
        name = f"{identifier(vm_id)}-{uuid.uuid4().hex}"
        path = self.root / f"{name}.tar"
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest, size = hashlib.sha256(), 0
            chunks, _ = container.get_archive(HOME + "/.", chunk_size=64 * 1024)
            with contextlib.closing(_Chunks(chunks)) as stream:
                descriptor, filename = tempfile.mkstemp(
                    prefix=".home-", suffix=".tmp", dir=self.root
                )
                temporary = Path(filename)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := stream.read(64 * 1024):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise StorageError("ホームのバックアップ容量が上限を超えました")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            entries = self.manifest(temporary)
            os.replace(temporary, path)
            temporary = None
            atomic_json(
                path.with_suffix(".json"),
                {
                    "format": 1,
                    "project": self.project,
                    "vm_id": vm_id,
                    "source_id": container.id,
                    "sha256": digest.hexdigest(),
                    "bytes": size,
                    "entries": len(entries),
                    "created_at": time.time(),
                },
            )
            return name
        except (OSError, tarfile.TarError) as error:
            raise StorageError(
                "ホームをバックアップできません。保存先の空き容量と権限を確認してください"
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _path(value):
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise StorageError("バックアップに扱えないパスがあります")
        return str(path)

    def manifest(self, source):
        result, hardlinks, symlinks = {}, {}, set()
        with tarfile.open(
            fileobj=source if hasattr(source, "read") else None,
            name=None if hasattr(source, "read") else source,
            mode="r|",
        ) as archive:
            for member in archive:
                path = self._path(member.name)
                if path in result or len(result) >= self.max_files:
                    raise StorageError(
                        "バックアップの項目が重複しているか、ファイル数が上限を超えています"
                    )
                entry = {"mode": member.mode, "uid": member.uid, "gid": member.gid}
                if member.isdir():
                    entry["type"] = "directory"
                elif member.isfile():
                    if member.size > self.max_bytes:
                        raise StorageError("バックアップのファイル容量が上限を超えています")
                    file = archive.extractfile(member)
                    digest = hashlib.sha256()
                    with file:
                        while chunk := file.read(64 * 1024):
                            digest.update(chunk)
                    entry.update(type="file", size=member.size, sha256=digest.hexdigest())
                elif member.issym():
                    entry.update(type="symlink", target=member.linkname)
                    symlinks.add(path)
                elif member.islnk():
                    hardlinks[path] = self._path(member.linkname)
                else:
                    raise StorageError("バックアップに通常ファイル以外の項目があります")
                result[path] = entry
        for path in result:
            if any(str(parent) in symlinks for parent in PurePosixPath(path).parents):
                raise StorageError("リンクを経由して書き込むバックアップは復元できません")
        pending = dict(hardlinks)
        while pending:
            resolved = [
                path
                for path, target in pending.items()
                if target in result
                and target not in pending
                and result[target].get("type") == "file"
            ]
            if not resolved:
                raise StorageError("バックアップのハードリンクが不正です")
            for path in resolved:
                result[path] = dict(result[pending.pop(path)])
        if result.get(".", {}).get("type") != "directory":
            raise StorageError("ホームのルート情報がないバックアップは復元できません")
        return result

    def verified(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}-[0-9a-f]{32}", name):
            raise ValueError("バックアップの識別子が不正です")
        path = self.root / f"{name}.tar"
        try:
            metadata = json.loads(path.with_suffix(".json").read_text())
            if metadata.get("format") != 1 or metadata.get("project") != self.project:
                raise StorageError("バックアップの所属または形式が一致しません")
            identifier(metadata["vm_id"])
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise StorageError("バックアップ容量が上限を超えています")
                    digest.update(chunk)
            if size != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]:
                raise StorageError("バックアップが破損しています。元の環境を削除しないでください")
            return path, metadata, self.manifest(path)
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
            raise StorageError("バックアップを検証できません") from error

    def seed(self, volumes: WorkspaceVolumes, volume, name):
        path, metadata, expected = self.verified(name)
        vm_id = metadata["vm_id"]
        if (
            metadata["project"] != volumes.project
            or volume.name != volumes.name_for(vm_id)
            or not volumes.owned(volume, vm_id)
        ):
            raise DockerUnavailableError("復元先のホーム保存先の所属が一致しません")
        volumes.require_unused(volume)
        with volumes.helper(volume) as helper:
            cleanup = (
                "from pathlib import Path; import shutil; root=Path('/home/agent'); "
                "[(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) "
                "for p in root.iterdir()]"
            )
            code, _ = helper.exec_run(["python", "-c", cleanup])
            if code != 0:
                raise StorageError("移行先のホームを準備できません")
            with path.open("rb") as source:
                if not helper.put_archive(HOME, source):
                    raise StorageError("ホームを復元できません")
            # Docker preserves child entries but keeps the mount root's ownership and mode.
            root = expected["."]
            attributes = (
                "import os,sys; os.chown('/home/agent',int(sys.argv[1]),int(sys.argv[2])); "
                "os.chmod('/home/agent',int(sys.argv[3]))"
            )
            code, _ = helper.exec_run(
                ["python", "-c", attributes, str(root["uid"]), str(root["gid"]), str(root["mode"])]
            )
            if code != 0:
                raise StorageError("ホームのアクセス権を復元できません")
            code, _ = helper.exec_run(["sync", "-f", HOME])
            if code != 0:
                raise StorageError("移行したホームをディスクへ保存できません")
            chunks, _ = helper.get_archive(HOME + "/.", chunk_size=64 * 1024)
            with contextlib.closing(io.BufferedReader(_Chunks(chunks))) as stream:
                actual = self.manifest(stream)
            if actual != expected:
                raise StorageError(
                    "移行後のファイル内容またはアクセス権が一致しません。元の環境を削除しないでください"
                )
