"""Bound HTTP bodies before parsing JSON or starting endpoint side effects."""

import asyncio

from starlette.responses import JSONResponse

HTTP_MAX_BODY_BYTES = 256 * 1024
HTTP_BODY_READ_TIMEOUT = 5.0


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes=HTTP_MAX_BODY_BYTES, read_timeout=HTTP_BODY_READ_TIMEOUT):
        self.app = app
        self.max_bytes = max_bytes
        self.read_timeout = read_timeout
        self.size_error = f"送信データは{max_bytes:,}バイト以内にしてください"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status, detail):
            headers = {"Cache-Control": "no-store"}
            if scope.get("http_version", "1.1").startswith("1."):
                # Stop unread bytes from being reused on this HTTP/1 connection.
                headers["Connection"] = "close"
            await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
                scope, receive, send
            )

        lengths = [v.strip() for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
        if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
            await reject(400, "Content-Lengthが不正です")
            return
        declared = None
        if lengths:
            value = lengths[0].lstrip(b"0") or b"0"
            # Reject enormous decimal values before converting them to an integer.
            if len(value) > len(str(self.max_bytes)) or int(value) > self.max_bytes:
                await reject(413, self.size_error)
                return
            declared = int(value)

        body = bytearray()
        too_large = False
        try:
            async with asyncio.timeout(self.read_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        too_large = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(
                408,
                f"送信データを{self.read_timeout:g}秒以内に受信できませんでした。接続を確認してもう一度操作してください",
            )
            return
        if too_large:
            await reject(413, self.size_error)
            return
        if declared is not None and len(body) != declared:
            await reject(400, "Content-Lengthと送信データの大きさが一致しません")
            return

        payload = bytes(body)
        body.clear()

        async def buffered_receive():
            nonlocal payload
            if payload is not None:
                message = {"type": "http.request", "body": payload, "more_body": False}
                payload = None
                return message
            return await receive()

        await self.app(scope, buffered_receive, send)
