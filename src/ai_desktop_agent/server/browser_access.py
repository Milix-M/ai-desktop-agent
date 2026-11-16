"""Browser access policy for the local-only desktop application."""

ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


class BrowserOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            origins = [v.decode("latin1") for k, v in scope.get("headers", []) if k == b"origin"]
            if origins and (len(origins) != 1 or origins[0] not in ALLOWED_ORIGINS):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"Origin not allowed"})
                return
        await self.app(scope, receive, send)
