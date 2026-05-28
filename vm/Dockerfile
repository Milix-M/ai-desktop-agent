# Stage 1: qcow2 イメージをビルド
FROM ubuntu:24.04 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    debootstrap \
    qemu-utils \
    e2fsprogs \
    qemu-user-static \
    && rm -rf /var/lib/apt/lists/*

ENV IMAGE_SIZE=20G

RUN mkdir -p /vm /rootfs
COPY prepare-image.sh /prepare-image.sh
RUN chmod +x /prepare-image.sh && /prepare-image.sh

# Stage 2: QEMU実行環境（最小）
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /vm

# ビルド済みイメージをコピー
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
