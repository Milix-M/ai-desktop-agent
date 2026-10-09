#!/bin/bash
# Desktop Entrypoint — Xfce + TigerVNC を起動するだけ
set -euo pipefail

VNC_GEOMETRY="${VNC_GEOMETRY:-1280x800}"
VNC_DEPTH="${VNC_DEPTH:-24}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# 残留ソケット掃除（再起動時の tigervnc 起動失敗を防ぐ）
rm -f /tmp/.X0-lock /tmp/.X11-unix/X0 2>/dev/null || true

log "Starting VNC desktop (geometry=${VNC_GEOMETRY}, depth=${VNC_DEPTH})"
# 認証なし（隔離ネットワーク前提。backend/vncdotool はパスワードなし接続）
exec su agent -c "vncserver :0 -geometry '$VNC_GEOMETRY' -depth '$VNC_DEPTH' -localhost no -SecurityTypes None --I-KNOW-THIS-IS-INSECURE -fg"
