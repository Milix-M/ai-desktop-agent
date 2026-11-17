#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="${1:?Usage: audit-images.sh OUTPUT_DIRECTORY}"
trivy="${TRIVY_BIN:-trivy}"
command -v "$trivy" >/dev/null
command -v jq >/dev/null
mkdir -p "$output"

names=(backend frontend desktop qmp android)
images=(
  "${BACKEND_SMOKE_IMAGE:-ai-desktop-agent-backend-smoke}"
  "${FRONTEND_SMOKE_IMAGE:-ai-desktop-agent-frontend-smoke}"
  "${DESKTOP_SMOKE_IMAGE:-ai-desktop-agent-desktop-smoke}"
  "${QMP_SMOKE_IMAGE:-ai-desktop-agent-qmp-smoke}"
  "${ANDROID_SMOKE_IMAGE:-ai-desktop-agent-android-smoke}"
)
families=(debian alpine ubuntu ubuntu ubuntu)
common=(--scanners vuln --pkg-types os --image-src docker --parallel 2 --timeout 5m
  --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL --list-all-pkgs --format json --exit-on-eol 1)
status=0
for i in "${!images[@]}"; do
  name="${names[$i]}"
  raw="$output/$name-all.json"
  gate="$output/$name-gate.json"
  # Keep unfiltered evidence, including findings without a published fix.
  if ! "$trivy" image "${common[@]}" --ignorefile '' --ignore-unfixed=false \
    --exit-code 0 --output "$raw" "${images[$i]}"; then
    echo "$name: OS scan failed" >&2
    status=1
    continue
  fi
  if ! jq -e --arg family "${families[$i]}" '
    .ArtifactType == "container_image" and .Metadata.OS.Family == $family
    and .Metadata.OS.EOSL != true
    and any(.Results[]?; .Class == "os-pkgs" and .Type == $family
      and (.Packages | type == "array" and length > 0))
  ' "$raw" >/dev/null; then
    echo "$name: missing, unexpected, or end-of-life OS inventory" >&2
    status=1
    continue
  fi
  # All fixable severities block CI, except the reviewed package/version/CVE entries.
  if ! "$trivy" image "${common[@]}" --ignore-unfixed --exit-code 1 \
    --ignorefile "$root/security/trivyignore.yaml" --show-suppressed \
    --output "$gate" "${images[$i]}"; then
    echo "$name: fixable vulnerabilities or scan failure (see $gate)" >&2
    status=1
  fi
done
exit "$status"
