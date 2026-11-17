FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends qemu-guest-agent \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["qemu-ga"]
