#!/usr/bin/env python3
"""Android-x86 ISO からカーネルと initrd を抽出する。

ISO直起動でも動作するが、カーネル直起動の方がgrub待ち（約60秒）を
スキップでき、約40秒でGUIに到達する。抽出物は `android/kernel` と
`android/initrd.img`（いずれもgit管理外）に配置する。

使い方:
    python3 scripts/extract-android-kernel.py [ISOパス]

外部依存なし（ISO9660の最小パーサ内蔵）。
"""
from __future__ import annotations

import struct
import subprocess
import tempfile
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ISO = REPO_ROOT / "android" / "android-x86_64.iso"
ANDROID_DIR = REPO_ROOT / "android"


def _parse_dir(data: bytes) -> list[tuple[bytes, int, int, int]]:
    """ディレクトリエントリを (name, extent, size, flags) の列で返す。"""
    recs = []
    off = 0
    while off < len(data):
        ln = data[off]
        if ln == 0:
            nxt = ((off // 2048) + 1) * 2048
            if nxt >= len(data):
                break
            off = nxt
            continue
        if off + ln > len(data):
            break
        rec = data[off : off + ln]
        nlen = rec[32]
        name = rec[33 : 33 + nlen]
        extent = struct.unpack("<I", rec[2:6])[0]
        size = struct.unpack("<I", rec[10:14])[0]
        flags = rec[25]
        recs.append((name, extent, size, flags))
        off += ln
    return recs


def _norm(name: bytes) -> str:
    """ISO9660名を正規化（バージョン除去・小文字化）。"""
    s = name.split(b"\x00")[0].split(b";")[0].decode("utf-8", errors="replace")
    return s.lower()


def find_root_files(iso: Path) -> dict[str, tuple[int, int]]:
    with open(iso, "rb") as f:
        f.seek(16 * 2048)
        pvd = f.read(2048)
        if pvd[1:6] != b"CD001":
            raise ValueError("ISO9660イメージではありません")
        root = pvd[156:190]
        loc = struct.unpack("<I", root[2:6])[0]
        dlen = struct.unpack("<I", root[10:14])[0]
        f.seek(loc * 2048)
        data = f.read(dlen)
    found: dict[str, tuple[int, int]] = {}
    for name, extent, size, flags in _parse_dir(data):
        if flags & 0x02:  # ディレクトリは除外
            continue
        found[_norm(name)] = (extent, size)
    return found


def extract(iso: Path, name: str, extent: int, size: int, dest: Path) -> None:
    with open(iso, "rb") as f:
        f.seek(extent * 2048)
        remaining = size
        with open(dest, "wb") as out:
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise IOError(f"{name} の読取中にEOF")
                out.write(chunk)
                remaining -= len(chunk)
    print(f"extracted {name} ({size} bytes) -> {dest}")


def main() -> int:
    iso = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ISO
    if not iso.is_file():
        print(f"ISOがありません: {iso}", file=sys.stderr)
        print("先に android/README.md の手順でISOを取得してください", file=sys.stderr)
        return 1
    names = ("kernel", "initrd.img")
    try:
        found = find_root_files(iso)
        with tempfile.TemporaryDirectory(dir=ANDROID_DIR) as temp:
            staging = Path(temp)
            for name in names:
                if name not in found or found[name][1] == 0:
                    raise ValueError(f"ISO内に {name} がありません")
                dest = ANDROID_DIR / name
                if dest.exists() and not dest.is_file():
                    raise ValueError(f"出力先が通常のファイルではありません: {dest}")
                extract(iso, name, *found[name], staging / name)
            subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts/patch-android-initrd.py"),
                 str(staging / "initrd.img")],
                check=True,
            )
            for name in names:
                (staging / name).replace(ANDROID_DIR / name)
    except (ValueError, OSError, struct.error, subprocess.CalledProcessError) as error:
        print(f"起動ファイルを準備できません: {error}", file=sys.stderr)
        return 1
    print("Androidの起動ファイルを準備しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
