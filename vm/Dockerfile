# QEMU VM ランタイム（最小）
# qcow2/kernel/initrd は build-vm-image.sh で事前ビルド → docker-compose volumes でマウント
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /vm

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV VM_IMAGE=/vm/desktop.qcow2 \
    VM_MEMORY=2048 \
    VM_VNC_PORT=5900 \
    CMDLINE_FILE=/vm/cmdline.txt

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=10 --timeout=3s --start-period=60s \
    CMD nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
