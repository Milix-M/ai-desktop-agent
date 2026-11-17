"""Single-operator access key and revocable browser sessions for HTTP and WebSocket."""

import asyncio
import contextlib
import hmac
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from ai_desktop_agent.privacy import protect, unprotect
from ai_desktop_agent.server.persistence import StorageError, atomic_text, metadata_lock

COOKIE_NAME = "ai_desktop_agent_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_SESSIONS = 128
AUTH_FAILURE_LIMIT = 10
AUTH_FAILURE_WINDOW = 60.0
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("GET", "/auth/status"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
}


@dataclass
class BrowserSession:
    expires: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class AuthenticationRateLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"認証の失敗が続いています。{retry_after}秒後にもう一度操作してください")


def authentication_rate_limit_response(error: AuthenticationRateLimitError) -> JSONResponse:
    return JSONResponse(
        {"detail": str(error)},
        status_code=429,
        headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
    )


class AccessControl:
    def __init__(self, key: str):
        if not (32 <= len(key) <= 256 and key.isascii() and not any(c.isspace() for c in key)):
            raise ValueError("AGENT_ACCESS_TOKENは空白を含まない32〜256文字のASCIIにしてください")
        self._key = key.encode()
        protect(key)
        self._sessions: dict[str, BrowserSession] = {}
        self._key_failures: deque[float] = deque()
        self._key_attempt_lock = threading.Lock()

    @classmethod
    def from_environment(cls):
        key = os.environ.get("AGENT_ACCESS_TOKEN", "")
        if not key:
            path = Path(os.environ.get("DATA_DIR", "./data")) / "auth" / "access-key"
            with metadata_lock(path):
                try:
                    key = path.read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    key = secrets.token_urlsafe(32)
                    atomic_text(path, key + "\n")
                except (OSError, UnicodeError) as error:
                    raise StorageError(f"接続キーを読み込めません: {path}") from error
        return cls(key)

    def valid_key(self, key: str) -> bool:
        return key.isascii() and hmac.compare_digest(self._key, key.encode("ascii"))

    def verify_key(self, key: str) -> bool:
        """Share a bounded failure window across every route using the operator key."""
        with self._key_attempt_lock:
            now = time.monotonic()
            while self._key_failures and self._key_failures[0] + AUTH_FAILURE_WINDOW <= now:
                self._key_failures.popleft()
            if len(self._key_failures) >= AUTH_FAILURE_LIMIT:
                raise AuthenticationRateLimitError(
                    max(1, math.ceil(self._key_failures[0] + AUTH_FAILURE_WINDOW - now))
                )
            if self.valid_key(key):
                self._key_failures.clear()
                return True
            self._key_failures.append(now)
            return False

    def new_session(self) -> str:
        self._prune()
        while len(self._sessions) >= MAX_SESSIONS:
            self.revoke(next(iter(self._sessions)))
        token = secrets.token_urlsafe(32)
        protect(token)
        self._sessions[token] = BrowserSession(time.monotonic() + SESSION_SECONDS)
        return token

    def _prune(self):
        for token, session in list(self._sessions.items()):
            if session.expires <= time.monotonic():
                self.revoke(token)

    def revoke(self, token: str):
        session = self._sessions.pop(token, None)
        if session is not None:
            session.revoked.set()
            unprotect(token)

    def authenticate(self, connection: HTTPConnection) -> tuple[bool, BrowserSession | None]:
        self._prune()
        session = self._sessions.get(connection.cookies.get(COOKIE_NAME, ""))
        authorization = connection.headers.get("authorization", "")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            try:
                if self.verify_key(value):
                    return True, None
            except AuthenticationRateLimitError:
                # A verified session remains usable while new key guesses are throttled.
                if session is None:
                    raise
        return session is not None, session


_access: AccessControl | None = None


def get_access() -> AccessControl:
    global _access
    if _access is None:
        _access = AccessControl.from_environment()
    return _access


class AuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and (scope["method"], scope["path"]) in PUBLIC_ROUTES:
            await self.app(scope, receive, send)
            return
        try:
            authenticated, session = get_access().authenticate(HTTPConnection(scope))
        except AuthenticationRateLimitError as error:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "認証の試行回数が上限に達しました",
                    }
                )
            else:
                await authentication_rate_limit_response(error)(scope, receive, send)
            return
        if not authenticated:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(
                    {"detail": "接続キーでログインしてください"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )(scope, receive, send)
            return
        if scope["type"] == "http":

            async def private_send(message):
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [
                            (k, v)
                            for k, v in message.get("headers", [])
                            if k.lower() != b"cache-control"
                        ]
                        + [(b"cache-control", b"no-store")],
                    }
                await send(message)

            await self.app(scope, receive, private_send)
            return
        if session is None:
            await self.app(scope, receive, send)
            return

        closed = False

        async def tracked_send(message):
            nonlocal closed
            if closed:
                return
            if message["type"] == "websocket.close":
                closed = True
            await send(message)

        async def watch_session():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(0, session.expires - time.monotonic())):
                    await session.revoked.wait()

        application = asyncio.create_task(self.app(scope, receive, tracked_send))
        expiration = asyncio.create_task(watch_session())
        try:
            done, _ = await asyncio.wait(
                {application, expiration}, return_when=asyncio.FIRST_COMPLETED
            )
            if application in done:
                await application
            elif not closed:
                await tracked_send(
                    {"type": "websocket.close", "code": 1008, "reason": "ログインが必要です"}
                )
        finally:
            for task in (application, expiration):
                task.cancel()
            await asyncio.gather(application, expiration, return_exceptions=True)
