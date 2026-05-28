FROM ubuntu:24.04

# QEMU/KVM + イメージ準備ツール
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    cloud-image-utils \
    wget \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /vm
COPY entrypoint.sh /entrypoint.sh
COPY cloud-init/ /cloud-init/
RUN chmod +x /entrypoint.sh

ENV VM_IMAGE=/vm/desktop.qcow2 \
    VM_MEMORY=4096 \
    VM_VNC_PORT=5900 \
    CLOUD_IMAGE_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=5 --timeout=3s \
    CMD echo | nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
