"""Own lazily created Docker connections without closing injected clients."""

import logging
import threading
from contextlib import contextmanager
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)


class DockerUnavailableError(RuntimeError):
    """Dockerへの接続・環境の照会・操作に失敗した場合のエラー。"""


def docker_operation(fn):
    @wraps(fn)
    def guarded(self, *args, **kwargs):
        with self._operation():
            return fn(self, *args, **kwargs)

    return guarded


class DockerClientOwner:
    _connection_error = "Docker ソケットに接続できません"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._owns_client = False
        self._client_lock = threading.Condition()
        self._operation_thread = threading.local()
        self._active_operations = 0
        self._closing = False
        self._closed = False

    @contextmanager
    def _operation(self):
        with self._client_lock:
            depth = getattr(self._operation_thread, "depth", 0)
            if self._closed or (self._closing and not depth):
                raise DockerUnavailableError("Docker接続は終了しています")
            self._operation_thread.depth = depth + 1
            self._active_operations += 1
        try:
            yield
        finally:
            with self._client_lock:
                self._operation_thread.depth -= 1
                self._active_operations -= 1
                if not self._active_operations:
                    self._client_lock.notify_all()

    def _docker(self) -> Any:
        with self._client_lock:
            if self._closed:
                raise DockerUnavailableError("Docker接続は終了しています")
            if self._client is not None:
                return self._client
            try:
                import docker
            except ImportError as error:
                raise DockerUnavailableError("docker パッケージが利用できません") from error
            client = None
            try:
                client = docker.from_env()
                client.ping()
            except BaseException as error:
                if client is not None:
                    self._close_client(client)
                if isinstance(error, Exception):
                    raise DockerUnavailableError(self._connection_error) from error
                raise
            self._client = client
            self._owns_client = True
            return client

    @staticmethod
    def _close_client(client: Any) -> None:
        try:
            client.close()
        except Exception:
            logger.exception("Docker接続の解放に失敗")

    def close(self) -> None:
        if getattr(self._operation_thread, "depth", 0):
            raise RuntimeError("実行中のDocker操作から接続を閉じることはできません")
        with self._client_lock:
            if self._closed:
                return
            self._closing = True
            self._client_lock.wait_for(lambda: not self._active_operations)
            self._closed = True
            if self._owns_client:
                client, self._client = self._client, None
                self._owns_client = False
                self._close_client(client)

    def __enter__(self):
        with self._client_lock:
            if self._closing:
                raise DockerUnavailableError("Docker接続は終了しています")
        return self

    def __exit__(self, *exc):
        self.close()
