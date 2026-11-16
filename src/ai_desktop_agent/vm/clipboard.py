"""Send UTF-8 text using the negotiated RFB Extended Clipboard extension.

https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst
Clipboard contents from the desktop are never requested or decompressed.
"""

import threading
import zlib
from struct import pack, unpack

from vncdotool.client import VNCDoToolClient, VNCDoToolFactory

EXTENDED_CLIPBOARD = 0xC0A1E5CE - (1 << 32)
TEXT = 1
CAPS = 1 << 24
REQUEST = 1 << 25
PEEK = 1 << 26
NOTIFY = 1 << 27
PROVIDE = 1 << 28
MAX_TEXT_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024


class ClipboardClient(VNCDoToolClient):
    def __init__(self):
        super().__init__()
        self.clipboard_ready = threading.Event()
        self.clipboard_provided = threading.Event()
        self.clipboard_supported = False
        self.clipboard_error = None
        self._clipboard_payload = None
        self._extended_cut = False
        self._retried_empty_update = False

    def setEncodings(self, encodings):  # noqa: N802
        super().setEncodings([*encodings, EXTENDED_CLIPBOARD])

    def connection_ready(self):
        return self

    def refreshScreen(self, incremental=False):  # noqa: N802
        self._retried_empty_update = False
        return super().refreshScreen(incremental)

    def _doConnection(self):  # noqa: N802
        empty = not self.rectangles and not self.rectanglePos
        super()._doConnection()
        if not empty or self.deferred is None:
            return
        # TigerVNC can answer the first request with only capability rectangles.
        # vncdotool drops that update without completing or renewing the request.
        if not self._retried_empty_update:
            self._retried_empty_update = True
            self.framebufferUpdateRequest(incremental=False)
        else:
            pending, self.deferred = self.deferred, None
            pending.errback(RuntimeError("VNCから画面画像を取得できません"))
            self.transport.loseConnection()

    def _send_clipboard(self, flags, payload=b""):
        message = pack("!I", flags) + payload
        self.transport.write(pack("!Bxxxi", 6, -len(message)) + message)

    def _invalid_clipboard(self):
        self.clipboard_supported = False
        self.clipboard_error = "VNCのクリップボード応答が不正です"
        self._clipboard_payload = None
        self.clipboard_ready.set()
        self.clipboard_provided.set()
        self.transport.loseConnection()

    def _handleServerCutText(self, block):  # noqa: N802
        (length,) = unpack("!xxxi", block)
        if abs(length) > MAX_MESSAGE_BYTES or (-4 < length < 0):
            self._invalid_clipboard()
            return
        self._extended_cut = length < 0
        if length:
            self.expect(self._handle_clipboard, abs(length))
        else:
            self.expect(self._handleConnection, 1)

    def _handle_clipboard(self, block):
        if self._extended_cut:
            (flags,) = unpack("!I", block[:4])
            if flags & CAPS:
                formats = flags & 0xFFFF
                if len(block) != 4 + 4 * formats.bit_count():
                    self._invalid_clipboard()
                    return
                required = TEXT | NOTIFY | PROVIDE
                self.clipboard_supported = flags & required == required
                # Receive requests and small notifications only. Do not request the
                # desktop's clipboard or accept unsolicited compressed contents.
                self._send_clipboard(CAPS | TEXT | REQUEST | NOTIFY, pack("!I", 0))
                self.clipboard_ready.set()
            elif self.clipboard_supported:
                action = flags & 0xFF000000
                if action == REQUEST and flags & TEXT and self._clipboard_payload is not None:
                    self._send_clipboard(PROVIDE | TEXT, self._clipboard_payload)
                    self.clipboard_provided.set()
                elif action == PEEK:
                    self._send_clipboard(NOTIFY | (TEXT if self._clipboard_payload else 0))
        self.expect(self._handleConnection, 1)

    def announce_text(self, text):
        # Return the protocol, as required by ThreadedVNCClientProxy's deferred chain.
        data = text.replace("\n", "\r\n").encode("utf-8") + b"\0"
        self._clipboard_payload = zlib.compress(pack("!I", len(data)) + data)
        self.clipboard_provided.clear()
        self._send_clipboard(NOTIFY | TEXT)
        return self

    def connectionLost(self, reason):  # noqa: N802
        self.clipboard_supported = False
        self.clipboard_error = "VNC接続が切断されました"
        self._clipboard_payload = None
        self.clipboard_ready.set()
        self.clipboard_provided.set()
        super().connectionLost(reason)


class ClipboardFactory(VNCDoToolFactory):
    protocol = ClipboardClient
