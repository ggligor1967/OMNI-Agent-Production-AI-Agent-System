#!/usr/bin/env bash
set -o pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker unavailable; probe skipped"
  exit 0
fi

image="$(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^(busybox|alpine|debian|ubuntu):' | head -n 1)"
if [ -z "$image" ]; then
  echo "No local shell-capable image found; probe skipped"
  exit 0
fi

echo "Using local image: $image"

docker run --rm --network none --entrypoint sh "$image" -c '
  echo PROBE_START
  echo INTERFACES
  ls /sys/class/net 2>/dev/null || true
  echo ROUTE_TABLE
  cat /proc/net/route 2>/dev/null || true
  route_lines=$(awk "NR>1 {count++} END {print count+0}" /proc/net/route 2>/dev/null || echo 0)
  echo ROUTE_LINE_COUNT="$route_lines"
  if [ "$route_lines" -eq 0 ]; then
    echo NETWORK_DISABLED_EVIDENCE=NO_ROUTES
  else
    echo NETWORK_DISABLED_EVIDENCE=ROUTES_PRESENT_CHECK_MANUALLY
  fi
'
