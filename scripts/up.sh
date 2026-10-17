#!/usr/bin/env bash
# KVMの有無を自動判定して compose を起動する。
# .env での KVM 指定は不要（USE_KVM 未設定時は自動切替）。
# 初期起動の作業環境は1つに絞る（複数同時起動は重いため）:
# - /dev/kvm あり: vm（QEMU/KVM）のみ。desktop/android は起動しない
# - /dev/kvm なし: desktop（軽量コンテナ）のみ。VM起動は制限される
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -e /dev/kvm ]; then
  echo "KVM detected (/dev/kvm) — starting vm only (single desktop to save resources)"
  export VNC_HOST=vm
  exec docker compose --profile kvm up -d --build "$@" vm websockify backend frontend
else
  echo "No KVM (/dev/kvm not found) — starting container desktop only"
  export VNC_HOST=desktop
  exec docker compose --profile container up -d --build "$@" desktop websockify-desktop backend frontend
fi
