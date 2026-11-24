#!/usr/bin/env bash
# Run on a disposable x86_64 Linux test host. Build and boot the production guest twice.
set -euo pipefail

kind=${1:?Usage: scripts/check-kvm-guest.sh linux|android}
case "$kind" in linux|android) ;; *) exit 2 ;; esac
test "$(uname -m)" = x86_64 && test -c /dev/kvm || {
    echo "An x86_64 host with /dev/kvm is required." >&2
    exit 1
}
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
work=$(mktemp -d)
project="guest-smoke-$(basename "$work" | tr '[:upper:].' '[:lower:]-')"
name="$project-vm"
compose=(docker compose --project-name "$project" --project-directory "$work" -f "$work/docker-compose.yml" --profile kvm)
cleanup() {
    status=$?
    set +e
    if (( status )); then
        docker logs --tail 100 "$name" 2>&1
        echo "Guest smoke files: $work" >&2
    fi
    if [[ "$kind" == android && -f "$work/docker-compose.yml" ]]; then
        "${compose[@]}" down --rmi local
    else
        docker rm -f "$name" >/dev/null 2>&1
        docker image rm "$project-linux" >/dev/null 2>&1
    fi
    if (( ! status )); then sudo rm -rf --one-file-system -- "$work"; fi
    exit "$status"
}
trap cleanup EXIT
if [[ "$kind" == linux ]]; then
    sudo VM_OUTPUT_DIR="$work" ./vm/build-vm-image.sh
    docker build -t "$project-linux" vm
    start_vm() {
        docker run -d --name "$name" --device /dev/kvm --user "$(id -u):$(id -g)" \
            --group-add "$(stat -c %g /dev/kvm)" --cap-drop NET_RAW \
            -v "$work:/vm" -e VM_BASE_IMAGE=/vm/desktop.qcow2 -e VM_IMAGE=/vm/saved.qcow2 \
            -e USE_KVM=true -e VM_CPUS=2 -e VM_MEMORY=3072 -p 127.0.0.1::5900 "$project-linux"
    }
    for phase in write read; do
        start_vm
        port=$(docker port "$name" 5900/tcp)
        uv run --frozen python scripts/verify-guest.py linux "$phase" "$work" "${port##*:}"
        docker rm -f "$name"
    done
else
    mkdir "$work/android" "$work/scripts"
    cp docker-compose.yml "$work/"
    cp android/{Dockerfile,entrypoint.sh,.dockerignore} "$work/android/"
    cp scripts/{extract-android-kernel.py,patch-android-initrd.py,mk-android-data.sh} "$work/scripts/"
    touch "$work/.env"
    iso="$work/android/android-x86_64.iso"
    if ! curl -fL --retry 2 --max-time 300 \
        https://downloads.sourceforge.net/project/android-x86/Release%209.0/android-x86_64-9.0-r2.iso -o "$iso"; then
        curl -fL --retry 2 --max-time 300 \
            https://archive.org/download/android-x86_64-9.0-r2/android-x86_64-9.0-r2.iso -o "$iso"
    fi
    printf '%s  %s\n' f7eb8fc56f29ad5432335dc054183acf086c539f3990f0b6e9ff58bd6df4604e "$iso" | sha256sum -c -
    python3 "$work/scripts/extract-android-kernel.py"
    "$work/scripts/mk-android-data.sh"
    "${compose[@]}" build android
    for phase in write read; do
        "${compose[@]}" up -d --no-deps --force-recreate android
        name=$("${compose[@]}" ps -q android)
        address=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
        uv run --frozen python scripts/verify-guest.py android "$phase" "$address" 5900
    done
fi
