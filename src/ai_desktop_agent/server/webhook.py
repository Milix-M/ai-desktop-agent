"""Deliver task completion notifications without blocking desktop execution."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager, suppress

import httpx

from ai_desktop_agent.privacy import protect
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.async_io import run_display_call

logger = logging.getLogger(__name__)


class TaskWebhook:
    def __init__(self, url: str | httpx.URL, secret: str, client: httpx.AsyncClient) -> None:
        self._url = url
        self._secret = secret.encode()
        self._client = client
        self._pending: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=128)

    async def task_finished(self, store: TaskStore, task_id: str, success: bool) -> None:
        record = await run_display_call(store.load, task_id)
        state = "completed" if success else "failed"
        if record is None or record.state != state or record.success is not success:
            logger.warning("Webhook skipped for task %s: terminal state was not saved", task_id)
            return
        event_id = f"task.finished:{record.id}"
        payload = {
            "event": "task.finished",
            "event_id": event_id,
            "task_id": record.id,
            "vm_id": record.vm_id,
            "state": record.state,
            "success": success,
            "finished_at": record.updated_at,
            "stop_reason": record.stop_reason,
            "result": [item.model_dump() for item in record.result],
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            self._pending.put_nowait((event_id, body))
        except asyncio.QueueFull:
            logger.warning("Webhook queue full, dropping %s", event_id)

    async def _deliver(self, event_id: str, body: bytes) -> None:
        digest = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Desktop-Agent-Event-Id": event_id,
            "X-Desktop-Agent-Signature": f"sha256={digest}",
        }
        for delay in (0, 1, 4):
            await asyncio.sleep(delay)
            try:
                async with (
                    asyncio.timeout(5),
                    self._client.stream(
                        "POST", self._url, content=body, headers=headers
                    ) as response,
                ):
                    if response.is_success:
                        return
                    status = response.status_code
            except (httpx.RequestError, TimeoutError):
                failure = "接続エラーまたはタイムアウト"
                continue
            failure = f"HTTP {status}"
            if status not in (408, 429) and status < 500:
                break
        logger.warning("Webhook delivery failed for %s (%s)", event_id, failure)

    async def run(self) -> None:
        while True:
            event_id, body = await self._pending.get()
            await self._deliver(event_id, body)


@asynccontextmanager
async def open_task_webhook():
    url = os.environ.get("TASK_WEBHOOK_URL", "").strip()
    if not url:
        yield None
        return
    secret = os.environ.get("TASK_WEBHOOK_SECRET", "")
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        raise ValueError("TASK_WEBHOOK_URL に有効な HTTP(S) URL を指定してください") from None
    if (
        target.scheme not in ("http", "https")
        or not target.host
        or target.userinfo
        or target.fragment
    ):
        raise ValueError(
            "TASK_WEBHOOK_URL に認証情報やフラグメントを含まない HTTP(S) URL を指定してください"
        )
    if len(secret) < 32:
        raise ValueError("TASK_WEBHOOK_SECRET に32文字以上を指定してください")
    protect(url)
    protect(str(target))
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        sender = TaskWebhook(target, secret, client)
        worker = asyncio.create_task(sender.run())
        try:
            yield sender
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
