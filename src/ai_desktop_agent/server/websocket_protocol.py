"""Keep protocol-level closing waits inside the application's cleanup budget."""

from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol


class AgentWebSocketProtocol(WebSocketProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # websockets may wait for several close phases and suppress cancellation.
        self.close_timeout = 1.0
