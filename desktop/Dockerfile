# 軽量コンテナ実行環境（QEMU不要のVNCデスクトップ）
# QEMU VM (vm サービス) の代替となる既定の作業環境。
# Xfce + TigerVNC で、backend は通常のVNCクライアントで操作する。
ARG CUA_DRIVER_ENABLED=false
FROM ubuntu:24.04 AS desktop-false

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

# Ubuntu's firefox package is a Snap launcher, which cannot run in this container.
# https://support.mozilla.org/en-US/kb/install-firefox-linux
ARG PACKAGE_REFRESH=manual
RUN : "$PACKAGE_REFRESH" && apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
        -o /etc/apt/keyrings/packages.mozilla.org.asc \
    && test "$(gpg --show-keys --with-colons /etc/apt/keyrings/packages.mozilla.org.asc \
        | awk -F: '$1 == "fpr" { print $10; exit }')" = "35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3" \
    && printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main' \
        > /etc/apt/sources.list.d/mozilla.list \
    && printf 'Package: firefox*\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n\nPackage: firefox\nPin: release o=Ubuntu\nPin-Priority: -1\n' \
        > /etc/apt/preferences.d/mozilla \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
    xfce4 \
    xfce4-terminal \
    thunar \
    mousepad \
    tigervnc-standalone-server \
    tigervnc-common \
    at-spi2-core \
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
    && firefox --version \
    && rm -rf /var/lib/apt/lists/*

# 作業ユーザー（VM側の agent と同様、NOPASSWD sudo）
RUN useradd -m -s /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

RUN mkdir -p /home/agent/.vnc \
    && printf '#!/bin/sh\nunset SESSION_MANAGER\nunset DBUS_SESSION_BUS_ADDRESS\nxset -dpms\nxset s off\nxset s noblank\nexec startxfce4\n' > /home/agent/.vnc/xstartup \
    && chmod +x /home/agent/.vnc/xstartup \
    && chown -R agent:agent /home/agent/.vnc

COPY licenses/Cua-Driver-MIT.txt /usr/share/doc/ai-desktop-agent/Cua-Driver-MIT.txt
COPY --chmod=755 install-cua-driver start-cua-driver /usr/local/bin/
ENV NO_AT_BRIDGE=0

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV VNC_GEOMETRY=1280x800 \
    VNC_DEPTH=24

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=10 --timeout=3s --start-period=30s \
    CMD nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]

# Calc exposes its native controls through AT-SPI when using the GTK VCL plugin.
FROM desktop-false AS desktop-true
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-gtk3 python3-gi gir1.2-atspi-2.0 \
    && rm -rf /var/lib/apt/lists/*
COPY calc-cells.py read-tables.py workspace-files.py /usr/local/lib/ai-desktop-agent/
RUN ln -s /usr/local/bin/start-cua-driver /usr/local/bin/start-calc-cells \
    && ln -s /usr/local/bin/start-cua-driver /usr/local/bin/read-gui-tables
ENV SAL_USE_VCLPLUGIN=gtk3 SAL_FORCE_ACCESSIBILITY=1

FROM desktop-${CUA_DRIVER_ENABLED} AS runtime
