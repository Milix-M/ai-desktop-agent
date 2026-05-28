"""接続先バックエンドの抽象インターフェース。

すべてのVM表示バックエンド（VNC, RDP, 直接X11など）は
このインターフェースを実装する。
"""

from abc import ABC, abstractmethod

from ai_desktop_agent.vm.screenshot import Screenshot


class DisplayBackend(ABC):
    """VMの画面表示と操作を受け持つバックエンド。

    エージェントはこのインターフェースを通じてVMとやり取りする。
    具体的な通信プロトコル（VNC等）はサブクラスに隠蔽される。
    """

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """接続中かどうか。"""
        ...

    @abstractmethod
    def connect(self, host: str, port: int = 5900, password: str | None = None) -> None:
        """VMに接続する。

        Args:
            host: VNCサーバーのホスト名またはIP。
            port: VNCポート番号。
            password: VNCパスワード（必要なら）。
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """VMから切断する。"""
        ...

    @abstractmethod
    def capture_screen(self) -> Screenshot:
        """画面全体のスクリーンショットを取得する。"""
        ...

    @abstractmethod
    def capture_region(self, x: int, y: int, width: int, height: int) -> Screenshot:
        """指定領域のスクリーンショットを取得する。"""
        ...

    # ── マウス操作 ─────────────────────────────────────

    @abstractmethod
    def mouse_move(self, x: int, y: int) -> None:
        """カーソルを (x, y) に移動。"""
        ...

    @abstractmethod
    def mouse_down(self, button: int = 1) -> None:
        """マウスボタンを押下 (1=左, 2=中, 3=右)。"""
        ...

    @abstractmethod
    def mouse_up(self, button: int = 1) -> None:
        """マウスボタンを解放。"""
        ...

    @abstractmethod
    def mouse_scroll(self, direction: str, amount: int) -> None:
        """スクロール。

        Args:
            direction: 'up' または 'down'。
            amount: スクロール量（クリック数）。
        """
        ...

    def mouse_click(self, x: int | None = None, y: int | None = None, button: int = 1) -> None:
        """指定座標をクリック（デフォルト実装: move → down → up）。"""
        if x is not None and y is not None:
            self.mouse_move(x, y)
        self.mouse_down(button)
        self.mouse_up(button)

    def mouse_double_click(
        self, x: int | None = None, y: int | None = None, button: int = 1
    ) -> None:
        """ダブルクリック（デフォルト実装: click → click）。"""
        self.mouse_click(x, y, button)
        self.mouse_click(button=button)

    def mouse_drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, button: int = 1
    ) -> None:
        """ドラッグ操作（デフォルト実装: move → down → move → up）。"""
        self.mouse_move(start_x, start_y)
        self.mouse_down(button)
        self.mouse_move(end_x, end_y)
        self.mouse_up(button)

    # ── キーボード操作 ─────────────────────────────────

    @abstractmethod
    def key_press(self, key: str) -> None:
        """キーを押して離す。"""
        ...

    @abstractmethod
    def key_down(self, key: str) -> None:
        """キーを押し続ける。"""
        ...

    @abstractmethod
    def key_up(self, key: str) -> None:
        """キーを離す。"""
        ...

    @abstractmethod
    def type_text(self, text: str) -> None:
        """テキストを入力する（ペースト使用）。"""
        ...

    def key_combo(self, keys: list[str]) -> None:
        """キーコンビネーション（デフォルト実装: 全部down → 逆順up）。"""
        for k in keys:
            self.key_down(k)
        for k in reversed(keys):
            self.key_up(k)
