# 軽量コンテナ実行環境（QEMU不要のVNCデスクトップ）
# QEMU VM (vm サービス) の代替となる既定の作業環境。
# Xfce + TigerVNC で、backend は通常のVNCクライアントで操作する。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    xfce4 \
    xfce4-terminal \
    thunar \
    mousepad \
    tigervnc-standalone-server \
    tigervnc-common \
    dbus-x11 \
    x11-xserver-utils \
    firefox \
    libreoffice-writer \
    libreoffice-calc \
    fonts-noto \
    fonts-noto-cjk \
    xdotool \
    wmctrl \
    netcat-openbsd \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# 作業ユーザー（VM側の agent と同様、NOPASSWD sudo）
RUN useradd -m -s /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

RUN mkdir -p /home/agent/.vnc \
    && printf '#!/bin/sh\nunset SESSION_MANAGER\nunset DBUS_SESSION_BUS_ADDRESS\nxset -dpms\nxset s off\nxset s noblank\nexec startxfce4\n' > /home/agent/.vnc/xstartup \
    && chmod +x /home/agent/.vnc/xstartup \
    && chown -R agent:agent /home/agent/.vnc

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV VNC_GEOMETRY=1280x800 \
    VNC_DEPTH=24

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=10 --timeout=3s --start-period=30s \
    CMD nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
