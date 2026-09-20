#!/usr/bin/env bash
# Keep the parent HTB at a fat 100 mbit so bit-rate is not the IV.
set -euo pipefail
SIG_BIT="100000000"
while (($#)); do
  case "$1" in
    --sig-bit) SIG_BIT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
HALF=$((SIG_BIT / 2))
docker exec b02-gateway4t4 sh -c "
  set -e
  tc class change dev eth0 parent 1: classid 1:1 htb rate ${SIG_BIT}bit
  tc class change dev eth0 parent 1:1 classid 1:10 htb rate ${HALF}bit ceil ${SIG_BIT}bit
  tc class change dev eth0 parent 1:1 classid 1:20 htb rate ${HALF}bit ceil ${SIG_BIT}bit
"
echo "cell rate set: link=${SIG_BIT}bit/s (not the experimental IV)"
