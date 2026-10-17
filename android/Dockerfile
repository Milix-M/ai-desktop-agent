# Android-x86 QEMU ランタイム
# Android-x86 ISO を QEMU/KVM で起動する。VNC操作は通常VMと同一。
# ISO 自体は容量のためイメージに含めず、docker-compose で volume マウントする。
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    dosfstools \
    mtools \
    e2fsprogs \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /android

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV ANDROID_ISO=/android/boot.iso \
    ANDROID_MEMORY=2048 \
    ANDROID_CPUS=4 \
    ANDROID_VNC_PORT=5900

EXPOSE 5900
HEALTHCHECK --interval=10s --retries=10 --timeout=3s --start-period=120s \
    CMD nc -z localhost 5900 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
