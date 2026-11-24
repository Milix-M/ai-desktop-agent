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

version_at_least() {
  [[ "$1" =~ ^v?([0-9]+)\.([0-9]+)([.-]|$) ]] || return 1
  local major=$((10#${BASH_REMATCH[1]})) minor=$((10#${BASH_REMATCH[2]}))
  (( major > $2 || (major == $2 && minor >= $3) ))
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
    printf 'Create %s/.env from .env.example and configure an LLM provider before starting.\n' "$PWD" >&2
    return 1
  fi
  local compose_version engine_api
  compose_version="$(docker compose version --short)"
  if ! version_at_least "$compose_version" 2 34; then
    echo "Docker Compose 2.34.0 or later is required for gw_priority (found $compose_version)." >&2
    return 1
  fi
  engine_api="$(docker version --format '{{.Server.APIVersion}}')"
  if ! version_at_least "$engine_api" 1 48; then
    echo "Docker Engine API 1.48 or later is required for gw_priority (found $engine_api)." >&2
    return 1
  fi
  local settings key value mode profile environment runtime=""
  # Resolve settings the same way as Compose, and retain only the keys used here.
  settings="$(docker compose config --environment | awk '/^(USE_KVM|DESKTOP_RUNTIME)=/')"
  while IFS='=' read -r key value; do
    case "$key" in
      USE_KVM) mode="$value" ;;
      DESKTOP_RUNTIME) runtime="$value" ;;
    esac
  done <<< "$settings"
  case "$runtime" in
    ""|runc|runsc) ;;
    *) echo 'DESKTOP_RUNTIME must be empty, runc, or runsc.' >&2; return 2 ;;
  esac
  mode="${mode:-auto}"
  mode="${mode,,}"
  profile="$(choose_profile "$mode" /dev/kvm "$(uname -m)")"
  if [[ "$runtime" == runsc && "$(docker info --format '{{if index .Runtimes "runsc"}}yes{{end}}')" != yes ]]; then
    echo 'DESKTOP_RUNTIME=runsc requires the runsc runtime to be registered with Docker.' >&2
    return 1
  fi
  if [[ "$profile" == kvm ]]; then
    export VNC_HOST=vm USE_KVM=true
    environment=vm
  else
    export VNC_HOST=desktop USE_KVM=false
    environment=desktop
  fi
  # Build before stopping anything. The guard preserves a verified home backup and
  # holds the update lock through Compose's replacement of the old container.
  docker compose --profile "$profile" build --pull \
    --build-arg "PACKAGE_REFRESH=$(date -u +%F)" "$environment" backend frontend
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
    printf 'Workspace preparation failed. Check the preceding error and %s/.env.\n' "$PWD" >&2
    return 1
  fi
  exec {guard_out}<&-
  if docker compose --profile "$profile" up -d --wait-timeout 300 "$@" --wait "$environment" backend frontend; then
    printf 'commit\n' >&"$guard_in"
    exec {guard_in}>&-
    wait "$guard_process"
    trap - EXIT
  else
    echo 'Startup did not complete. Check docker compose ps -a and docker compose logs backend frontend.' >&2
    return 1
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
