#!/usr/bin/env bash
# Set the shared-link rate for one cell.  The parent class 1:1 IS the shared
# link (rate = sig-bit).  Signaling (class 1:10, dst port 9701) is guaranteed
# half and may borrow to the full link; background (class 1:20, iperf3 port
# 5201) likewise.  With no background traffic signaling gets the full rate;
# with saturating background it falls back toward its guarantee.
set -euo pipefail
SIG_BIT=""
while (($#)); do
  case "$1" in
    --sig-bit) SIG_BIT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
[ -n "$SIG_BIT" ] || { echo "--sig-bit required" >&2; exit 1; }
HALF=$((SIG_BIT / 2))
docker exec gateway sh -c "
  set -e
  tc class change dev eth0 parent 1: classid 1:1 htb rate ${SIG_BIT}bit
  tc class change dev eth0 parent 1:1 classid 1:10 htb rate ${HALF}bit ceil ${SIG_BIT}bit
  tc class change dev eth0 parent 1:1 classid 1:20 htb rate ${HALF}bit ceil ${SIG_BIT}bit
"
echo "cell rate set: link=${SIG_BIT}bit/s (sig guaranteed ${HALF}, ceil ${SIG_BIT})"
