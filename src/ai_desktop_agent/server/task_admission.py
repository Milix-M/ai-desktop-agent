"""Bound task submissions before reading bodies or waiting for environment locks."""

import math
import time
from collections import deque
from contextlib import contextmanager

from starlette.responses import JSONResponse

TASK_SUBMISSION_LIMIT = 60
TASK_SUBMISSION_WINDOW = 60.0
MAX_PENDING_TASK_SUBMISSIONS = 8


class TaskAdmissionError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(
            f"タスクの受付が混み合っています。{retry_after}秒後にもう一度送信してください"
        )


class TaskAdmission:
    """One operator shares capacity across cookies, bearer keys, and client addresses.

    Entered only on the server event loop; no await occurs while updating capacity.
    """

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._accepted: deque[float] = deque()
        self._pending = 0

    @contextmanager
    def hold(self):
        now = self.clock()
        while self._accepted and self._accepted[0] + TASK_SUBMISSION_WINDOW <= now:
            self._accepted.popleft()
        if len(self._accepted) >= TASK_SUBMISSION_LIMIT:
            raise TaskAdmissionError(
                max(1, math.ceil(self._accepted[0] + TASK_SUBMISSION_WINDOW - now))
            )
        if self._pending >= MAX_PENDING_TASK_SUBMISSIONS:
            raise TaskAdmissionError(1)
        self._accepted.append(now)
        self._pending += 1
        try:
            yield
        finally:
            self._pending -= 1


_task_admission = TaskAdmission()


class TaskAdmissionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/tasks":
            await self.app(scope, receive, send)
            return
        try:
            with _task_admission.hold():
                await self.app(scope, receive, send)
        except TaskAdmissionError as error:
            headers = {"Retry-After": str(error.retry_after), "Cache-Control": "no-store"}
            if scope.get("http_version", "1.1").startswith("1."):
                headers["Connection"] = "close"
            await JSONResponse({"detail": str(error)}, status_code=429, headers=headers)(
                scope, receive, send
            )
