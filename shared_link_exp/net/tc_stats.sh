#!/usr/bin/env bash
# Snapshot tc queue/class counters on the gateway's eth0.
set -euo pipefail
docker exec gateway sh -c 'tc -s qdisc show dev eth0; echo ---; tc -s class show dev eth0'
