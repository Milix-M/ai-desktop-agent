#!/usr/bin/env bash
# Android用APKを取得する（フリーのアプリストア同梱用）。
# ゲストにネットが無い前提で、ホスト側でAPKを落として android/apks/ に置く。
# 起動時にentrypointがFATイメージ化してUSBストレージとして接続する。
# 使い方: ./scripts/fetch-apks.sh [--force]
set -euo pipefail

cd "$(dirname "$0")/.."

FORCE=false
[ "${1:-}" = "--force" ] && FORCE=true

mkdir -p android/apks

fetch() {
    local name="$1"
    local url="$2"
    local dest="android/apks/$name"
    if [ -f "$dest" ] && [ "$FORCE" != "true" ]; then
        echo "skip (exists): $name"
        return 0
    fi
    echo "fetch: $name"
    curl -fSL --retry 3 -o "$dest" "$url"
    # APKマジック確認（PK zip）
    if ! head -c2 "$dest" | grep -q "PK"; then
        echo "ERROR: $dest はAPKではありません（HTML等の可能性）"
        rm -f "$dest"
        return 1
    fi
    ls -lh "$dest"
}

# F-Droid（フリーのアプリストア本体。Aurora Store等もここから入手可）
fetch "F-Droid.apk" "https://f-droid.org/F-Droid.apk"

echo "done. android/apks/ にAPKを追加すれば起動時にUSB経由で持ち込めます。"
