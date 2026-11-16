"""Browser access policy for the local-only desktop application."""

import ipaddress
import os

ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


class ManagementNetworkMiddleware:
    def __init__(self, app):
        self.app = app
        self.networks = [
            ipaddress.ip_network(value.strip())
            for value in os.environ.get("AGENT_MANAGEMENT_CIDRS", "127.0.0.0/8,::1/128").split(",")
        ]

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            peer = (scope.get("client") or ("", 0))[0]
            try:
                address = ipaddress.ip_address(peer)
                allowed = any(address in network for network in self.networks)
            except ValueError:
                allowed = False
            if not allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send(
                        {"type": "http.response.body", "body": b"Management network required"}
                    )
                return
        await self.app(scope, receive, send)


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
