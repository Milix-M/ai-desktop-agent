#!/usr/bin/env bash
# Select one initial environment. Source this file to test selection without starting Docker.
set -euo pipefail

choose_profile() {
  local mode="$1" device="$2" arch="$3" file
  case "$mode" in auto|true|false) ;; *) echo 'USE_KVM must be auto, true, or false' >&2; return 2 ;; esac
  if [[ "$mode" == false ]]; then
    echo container
    return
  fi
  if [[ ! -e "$device" || ( "$arch" != x86_64 && "$arch" != amd64 ) ]]; then
    if [[ "$mode" == true ]]; then
      echo 'USE_KVM=true requires an x86_64 host with /dev/kvm. Use USE_KVM=false for a container desktop.' >&2
      return 1
    fi
    echo container
    return
  fi
  for file in vm/desktop.qcow2 vm/vmlinuz vm/initrd.img vm/cmdline.txt; do
    if [[ ! -f "$file" || ! -s "$file" ]]; then
      if [[ "$mode" == true ]]; then
        echo "Missing VM file: $file. Prepare the guest with sudo ./vm/build-vm-image.sh or use USE_KVM=false." >&2
        return 1
      fi
      echo "VM files are not ready ($file); starting the container desktop." >&2
      echo container
      return
    fi
  done
  echo kvm
}

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  if [[ ! -f .env ]]; then
    echo 'Create .env from .env.example and configure an LLM provider before starting.' >&2
    return 1
  fi
  local mode profile
  # Compose resolves .env, quoted values, overrides, and shell precedence. Print only this key.
  mode="$(docker compose config --environment | awk -F= '$1 == "USE_KVM" { print substr($0, index($0, "=") + 1) }')"
  mode="${mode:-auto}"
  mode="${mode,,}"
  profile="$(choose_profile "$mode" /dev/kvm "$(uname -m)")"
  if [[ "$profile" == kvm ]]; then
    export VNC_HOST=vm USE_KVM=true
    exec docker compose --profile kvm up -d --build "$@" vm backend frontend
  else
    export VNC_HOST=desktop USE_KVM=false
    exec docker compose --profile container up -d --build "$@" desktop backend frontend
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
