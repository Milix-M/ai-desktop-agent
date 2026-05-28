# Stage 1: qcow2 イメージをビルド
# --platform=linux/amd64 により、ARM64ホストでもQEMUエミュレーションで動作
FROM --platform=linux/amd64 ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# 基本ツール
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-utils \
    e2fsprogs \
    locales \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# カーネル + 基本システム
RUN apt-get update && apt-get install -y --no-install-recommends \
    linux-image-generic \
    initramfs-tools \
    udev \
    dbus \
    network-manager \
    sudo \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# KDE Plasma 最小デスクトップ
RUN apt-get update && apt-get install -y --no-install-recommends \
    kde-plasma-desktop \
    konsole \
    firefox \
    xdotool \
    wmctrl \
    xauth \
    dolphin \
    && rm -rf /var/lib/apt/lists/*

# agent ユーザーと自動ログイン設定
RUN useradd -m -s /bin/bash -G sudo agent \
    && echo "agent:agent" | chpasswd \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent

RUN mkdir -p /etc/systemd/system/getty@tty1.service.d
RUN echo '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin agent --noclear %I $TERM' \
    > /etc/systemd/system/getty@tty1.service.d/autologin.conf

RUN echo 'if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then\n    exec startplasma-x11\nfi' \
    > /home/agent/.profile \
    && chown agent:agent /home/agent/.profile

# systemd ターゲットを graphical.target に
RUN systemctl set-default graphical.target \
    && systemctl enable getty@tty1 \
    && systemctl enable NetworkManager

# ── rootfs から ext4 → qcow2 イメージを作成 ──
COPY build-image.sh /build-image.sh
RUN chmod +x /build-image.sh && /build-image.sh

# Stage 2: QEMU実行環境（最小 — ホストアーキテクチャ）
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /vm

COPY --from=builder /vm/desktop.qcow2 /vm/desktop.qcow2
COPY --from=builder /vm/vmlinuz /vm/vmlinuz
COPY --from=builder /vm/initrd.img /vm/initrd.img
COPY --from=builder /vm/cmdline.txt /vm/cmdline.txt

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV VM_IMAGE=/vm/desktop.qcow2 \
    VM_MEMORY=2048 \
    VM_VNC_PORT=5900 \
    CMDLINE_FILE=/vm/cmdline.txt

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=5 --timeout=3s \
    CMD echo | nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
