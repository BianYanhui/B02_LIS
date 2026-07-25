#!/usr/bin/env bash
# Snapshot tc queue/class counters on the isolated 4T4 gateway's eth0.
set -euo pipefail
docker exec b02-gateway4t4 sh -c 'tc -s qdisc show dev eth0; echo ---; tc -s class show dev eth0'
