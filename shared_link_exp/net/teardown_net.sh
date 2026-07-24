#!/usr/bin/env bash
# Tear down ONLY the b02 shared-link platform (our containers + network).
set -euo pipefail
for c in gateway bgserver; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    docker rm -f "$c" >/dev/null
  fi
done
if docker network inspect b02-net >/dev/null 2>&1; then
  docker network rm b02-net >/dev/null
fi
echo "b02-net torn down (gateway, bgserver, network removed; image b02-gw kept)"
