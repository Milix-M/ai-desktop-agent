"""Isolate blocking VNC calls and close their transport on timeout or cancellation."""

import socket
import threading
import time
from concurrent.futures import Future

from twisted.internet import reactor
from twisted.internet.defer import maybeDeferred
from twisted.internet.endpoints import HostnameEndpoint, UNIXClientEndpoint
from vncdotool.api import ThreadedVNCClientProxy

from ai_desktop_agent.vm.clipboard import ClipboardClient, ClipboardFactory


class ManagedVNCProtocol(ClipboardClient):
    def connectionMade(self):  # noqa: N802
        super().connectionMade()
        self.factory.owner.transport_connected(self)

    def connectionLost(self, reason):  # noqa: N802
        pending, self.deferred = self.deferred, None
        if pending is not None and not pending.called:
            pending.errback(reason)
        super().connectionLost(reason)


class ManagedVNCFactory(ClipboardFactory):
    protocol = ManagedVNCProtocol

    def clientConnectionMade(self, protocol):  # noqa: N802
        if not self.deferred.called:
            self.deferred.callback(protocol)

    def clientConnectionFailed(self, connector, reason):  # noqa: N802
        if not self.deferred.called:
            self.deferred.errback(reason)
        self.owner.connection_failed(reason)

    def clientConnectionLost(self, connector, reason):  # noqa: N802
        self.owner.connection_failed(reason)


class ManagedVNCProxy(ThreadedVNCClientProxy):
    def __init__(self, factory, timeout=10.0):
        super().__init__(factory, timeout)
        self._state_lock = threading.RLock()
        self._call_lock = threading.Lock()
        self._closed = False
        self._ready = Future()
        self._pending = {}
        self._connection = None
        factory.owner = self
        factory.deferred.addCallbacks(self._connected, self.connection_failed)

    @property
    def closed(self):
        with self._state_lock:
            return self._closed

    def connect(self, host, port=5900, family=socket.AF_INET):
        reactor.callFromThread(self._connect, host, port, family)

    def _connect(self, host, port, family):
        with self._state_lock:
            if self._closed:
                return
            try:
                if family in {socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6}:
                    endpoint = HostnameEndpoint(reactor, host, port, timeout=self.timeout)
                elif family == getattr(socket, "AF_UNIX", None):
                    endpoint = UNIXClientEndpoint(reactor, host, timeout=self.timeout)
                else:
                    raise ValueError("Unsupported VNC socket family")
                self._connection = endpoint.connect(self.factory)
                self._connection.addErrback(self.connection_failed)
            except Exception as error:
                self._abort(error)

    def transport_connected(self, protocol):
        with self._state_lock:
            self.protocol = protocol
            if self._closed:
                self._close_transport([])

    def _connected(self, protocol):
        with self._state_lock:
            if not self._closed and not self._ready.done():
                self._ready.set_result(protocol)
        return protocol

    def connection_failed(self, reason):
        self._abort(reason.value)
        return None  # Consume Twisted failures after delivering them to the calling thread.

    def disconnect(self):
        self._abort(ConnectionError("VNC接続を切断しました"))

    def _abort(self, error):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            if not self._ready.done():
                self._ready.set_exception(error)
            pending = list(self._pending.items())
            for future, _ in pending:
                if not future.done():
                    future.set_exception(error)
        reactor.callFromThread(self._close_transport, [d for _, d in pending if d is not None])

    def _close_transport(self, pending):
        with self._state_lock:
            connection, protocol = self._connection, self.protocol
            if connection is not None and not connection.called:
                connection.cancel()
            if protocol is not None:
                protocol.transport.abortConnection()
                if protocol.deferred in pending:
                    protocol.deferred = None
            for deferred in pending:
                deferred.cancel()

    def __getattr__(self, name):
        method = getattr(self.factory.protocol, name)
        if not callable(method):
            return getattr(self.protocol, name)

        def call(*args, **kwargs):
            deadline = time.monotonic() + self.timeout
            if not self._call_lock.acquire(timeout=self.timeout):
                error = TimeoutError("VNC操作の応答待ちが上限に達しました")
                self._abort(error)
                raise error
            future = Future()
            try:
                if self.closed:
                    raise ConnectionError("VNC接続は切断されています")
                protocol = self._ready.result(timeout=max(0, deadline - time.monotonic()))
                with self._state_lock:
                    if self._closed:
                        raise ConnectionError("VNC接続は切断されています")
                    self._pending[future] = None
                reactor.callFromThread(self._invoke, future, protocol, method, args, kwargs)
                return future.result(timeout=max(0, deadline - time.monotonic()))
            except TimeoutError as error:
                self._abort(error)
                raise TimeoutError("VNC操作の応答待ちが上限に達しました") from error
            finally:
                with self._state_lock:
                    self._pending.pop(future, None)
                self._call_lock.release()

        return call

    def _invoke(self, future, protocol, method, args, kwargs):
        with self._state_lock:
            if self._closed or future.done():
                return
            deferred = maybeDeferred(method, protocol, *args, **kwargs)
            self._pending[future] = deferred
            deferred.addCallbacks(
                lambda result: self._finish(future, result),
                lambda reason: self._finish(future, reason.value, failed=True),
            )

    def _finish(self, future, result, *, failed=False):
        with self._state_lock:
            if not future.done():
                if failed:
                    future.set_exception(result)
                else:
                    future.set_result(result)
        return None if failed else result
