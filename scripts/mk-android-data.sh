#!/usr/bin/env bash
# Android用永続dataイメージを作成する。
# Live起動は /data がtmpfsのため再起動で設定が消える。
# あらかじめext4化したイメージを用意し、entrypointが
# `-drive ... + DATA=vda` でゲストの /data としてマウントする。
# 以降はタイムゾーン・ランチャー既定等の設定が再起動を超えて残る。
# 使い方: ./scripts/mk-android-data.sh [サイズMB, 既定1024]
set -euo pipefail

cd "$(dirname "$0")/.."

SIZE_MB="${1:-1024}"
IMG="android/android-data.img"

if [[ ! "$SIZE_MB" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: サイズは正の整数（MiB）で指定してください" >&2
    exit 1
fi
if [ -e "$IMG" ] || [ -L "$IMG" ]; then
    echo "ERROR: $IMG は既に存在します。既存の保存内容を確認してください" >&2
    exit 1
fi

# Publish only a complete filesystem. Concurrent initializations cannot replace it.
tmp=$(mktemp "$(dirname "$IMG")/.android-data.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT
truncate -s "${SIZE_MB}M" "$tmp"
mkfs.ext4 -q -L android-data -F "$tmp"
ln -T -- "$tmp" "$IMG"
ls -lh "$IMG"
