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
  for file in vm/desktop.qcow2; do
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

close_upgrade_guard() {
  local input="$1" process="$2"
  exec {input}>&- || true
  wait "$process" 2>/dev/null || true
}

main() {
  local argument
  for argument in "$@"; do
    case "$argument" in
      --no-recreate=false|--no-start=false) ;;
      --no-recreate|--no-recreate=*|--no-start|--no-start=*)
        echo 'This update must recreate and start the environment. Remove --no-recreate and --no-start.' >&2
        return 2
        ;;
    esac
  done
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  if [[ ! -f .env ]]; then
    echo 'Create .env from .env.example and configure an LLM provider before starting.' >&2
    return 1
  fi
  local mode profile environment
  # Compose resolves .env, quoted values, overrides, and shell precedence. Print only this key.
  mode="$(docker compose config --environment | awk -F= '$1 == "USE_KVM" { print substr($0, index($0, "=") + 1) }')"
  mode="${mode:-auto}"
  mode="${mode,,}"
  profile="$(choose_profile "$mode" /dev/kvm "$(uname -m)")"
  if [[ "$profile" == kvm ]]; then
    export VNC_HOST=vm USE_KVM=true
    environment=vm
  else
    export VNC_HOST=desktop USE_KVM=false
    environment=desktop
  fi
  # Build before stopping anything. The guard preserves a verified home backup and
  # holds the update lock through Compose's replacement of the old container.
  docker compose --profile "$profile" build "$environment" backend frontend
  coproc UPGRADE { docker compose --profile "$profile" run --rm -T --no-deps \
    --entrypoint /app/.venv/bin/python backend \
    -m ai_desktop_agent.server.workspace_migration prepare --profile "$profile"; }
  local guard_process="$UPGRADE_PID" original_in="${UPGRADE[1]}" original_out="${UPGRADE[0]}"
  local guard_in guard_out marker
  exec {guard_in}>&"$original_in"
  exec {guard_out}<&"$original_out"
  exec {original_in}>&-
  exec {original_out}<&-
  trap "close_upgrade_guard $guard_in $guard_process" EXIT
  if ! IFS= read -r marker <&"$guard_out" || [[ "$marker" != WORKSPACE_READY ]]; then
    echo 'Workspace backup failed. The environment has not been replaced.' >&2
    return 1
  fi
  exec {guard_out}<&-
  if docker compose --profile "$profile" up -d "$@" "$environment" backend frontend; then
    printf 'commit\n' >&"$guard_in"
    exec {guard_in}>&-
    wait "$guard_process"
    trap - EXIT
  else
    return 1
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
