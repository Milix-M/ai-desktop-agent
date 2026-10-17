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

if [ -d "$IMG" ]; then
    # composeのvolumeマウントが空ディレクトリを作っていた場合の救済
    rmdir "$IMG" 2>/dev/null || {
        echo "ERROR: $IMG が空でないディレクトリです。手動で確認してください"
        exit 1
    }
fi
if [ -e "$IMG" ]; then
    echo "ERROR: $IMG は既に存在します（作り直す場合は削除してから実行）"
    exit 1
fi

# 通常ファイルへのmkfsはroot不要
mkfs.ext4 -L android-data -F "$IMG" "${SIZE_MB}M"
ls -lh "$IMG"
echo "作成しました。次回起動から /data としてマウントされます。"
echo "初回はエージェント等でタイムゾーン・ランチャー既定を設定してください。"
