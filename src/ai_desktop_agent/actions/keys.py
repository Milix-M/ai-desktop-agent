"""Key names shared by model action parsing and VNC input."""

# キー名マッピング: Actionキー名 → vncdotoolキー名
KEY_NAMES: dict[str, str] = {
    "enter": "enter",
    "return": "enter",
    "escape": "escape",
    "esc": "escape",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "home": "home",
    "end": "end",
    "page_up": "page_up",
    "page_down": "page_down",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "f1": "f1",
    "f2": "f2",
    "f3": "f3",
    "f4": "f4",
    "f5": "f5",
    "f6": "f6",
    "f7": "f7",
    "f8": "f8",
    "f9": "f9",
    "f10": "f10",
    "f11": "f11",
    "f12": "f12",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "super": "super",
    "win": "super",
    "cmd": "super",
    "insert": "insert",
    "print_screen": "print_screen",
    # Android-x86 のウェイク用。公式docでは ESC / Menu / 矢印のみが
    # スリープ復帰キーとして定義されている（Shift は復帰しない）。
    "menu": "menu",
}


_MODIFIERS = frozenset({"ctrl", "alt", "shift", "super"})


def parse_shortcut(key: str) -> list[str]:
    """Parse a plus-delimited sequence of distinct modifiers and one supported key."""
    parts = [part.strip(" ").lower() for part in key.split("+")]
    keys = [KEY_NAMES.get(part, part) for part in parts]
    modifiers, final = keys[:-1], keys[-1]
    if (
        not key.isascii()
        or not modifiers
        or not set(modifiers) <= _MODIFIERS
        or len(set(modifiers)) != len(modifiers)
        or final in _MODIFIERS
        or not (final in KEY_NAMES or (len(final) == 1 and final.isprintable()))
    ):
        raise ValueError("ショートカットには修飾キーと1つのキーを指定してください")
    return keys
