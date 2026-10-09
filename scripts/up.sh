#!/usr/bin/env bash
# KVMの有無を自動判定して compose を起動する。
# .env での KVM 指定は不要（USE_KVM 未設定時は自動切替）。
# - /dev/kvm あり: kvm プロファイル付きで起動（vm + desktop の両方が利用可）
# - /dev/kvm なし: desktop（軽量コンテナ）のみ起動。VM起動は制限される
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -e /dev/kvm ]; then
  echo "KVM detected (/dev/kvm) — starting with vm profile"
  exec docker compose --profile kvm up -d --build "$@"
else
  echo "No KVM (/dev/kvm not found) — starting container-only (desktop)"
  exec docker compose up -d --build "$@"
fi
