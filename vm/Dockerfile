# QEMU VM ランタイム（最小）
# qcow2 は事前ビルドし、各ディスクから対応するカーネルとinitrdを取得する。
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    e2fsprogs \
    util-linux \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /vm

COPY entrypoint.sh /entrypoint.sh
COPY prepare-boot.sh /prepare-boot.sh
RUN chmod +x /entrypoint.sh

ENV VM_IMAGE=/vm/desktop.qcow2 \
    VM_MEMORY=4096 \
    VM_CPUS=4 \
    VM_VNC_PORT=5900

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=10 --timeout=3s --start-period=60s \
    CMD nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
