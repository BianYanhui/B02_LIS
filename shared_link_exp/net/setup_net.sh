#!/usr/bin/env bash
# Build the b02-net docker network, b02-gw image, gateway + bgserver
# containers, and the tc HTB hierarchy on gateway eth0.  Idempotent: safe to
# re-run (our own two containers are recreated, nothing else is touched).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIG_BIT=100000   # placeholder signaling link rate; cells override via cell_rate.sh
NETEM_DELAY=0    # optional base delay in ms on the signaling class
MTU=296          # keep TCP's minimum congestion window below the tiny test BDP
REBUILD=0

while (($#)); do
  case "$1" in
    --sig-bit) SIG_BIT="$2"; shift 2;;
    --netem-delay) NETEM_DELAY="$2"; shift 2;;
    --mtu) MTU="$2"; shift 2;;
    --rebuild) REBUILD=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

if ! docker network inspect b02-net >/dev/null 2>&1; then
  docker network create --subnet 172.30.0.0/24 b02-net >/dev/null
fi
# docker does not report an explicit Gateway when it assigns the first
# address of the subnet; read it from the host-side bridge interface instead.
NET_ID="$(docker network inspect b02-net -f '{{.Id}}')"
BRIDGE_IP="$(ip -4 -o addr show dev "br-${NET_ID:0:12}" | awk '{print $4}' | cut -d/ -f1)"
[ -n "$BRIDGE_IP" ] || { echo "cannot determine b02-net bridge IP" >&2; exit 1; }

if ((REBUILD)) || ! docker image inspect b02-gw >/dev/null 2>&1; then
  docker build -t b02-gw -f "$HERE/Dockerfile" "$HERE" >/dev/null
fi

# Recreate ONLY our own containers (fixed names).
for c in gateway bgserver; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    docker rm -f "$c" >/dev/null
  fi
done

docker run -d --name gateway --network b02-net --cap-add NET_ADMIN \
  -p 127.0.0.1:9700:9700 \
  b02-gw --listen 9700 --downstream "${BRIDGE_IP}:9701" >/dev/null

docker run -d --name bgserver --network b02-net \
  --entrypoint iperf3 b02-gw -s -p 5201 >/dev/null

HALF=$((SIG_BIT / 2))
docker exec gateway sh -c "
  set -e
  # At a few kbit/s, the default 1500-byte MTU makes TCP's minimum cwnd a
  # multi-second standing queue. Frames are only 104 bytes on the wire, so
  # a 296-byte MTU remains lossless while exposing load-driven HTB queueing.
  ip link set dev eth0 mtu ${MTU}
  tc qdisc replace dev eth0 root handle 1: htb default 20
  tc class add dev eth0 parent 1: classid 1:1 htb rate ${SIG_BIT}bit
  tc class add dev eth0 parent 1:1 classid 1:10 htb rate ${HALF}bit ceil ${SIG_BIT}bit
  tc class add dev eth0 parent 1:1 classid 1:20 htb rate ${HALF}bit ceil ${SIG_BIT}bit
  if [ \"$NETEM_DELAY\" != \"0\" ]; then
    tc qdisc add dev eth0 parent 1:10 handle 10: netem delay ${NETEM_DELAY}ms limit 1000
  else
    tc qdisc add dev eth0 parent 1:10 handle 10: bfifo limit 65536
  fi
  tc qdisc add dev eth0 parent 1:20 handle 20: bfifo limit 65536
  tc filter add dev eth0 protocol ip parent 1:0 prio 1 u32 match ip dport 9701 0xffff flowid 1:10
  tc filter add dev eth0 protocol ip parent 1:0 prio 2 u32 match ip dport 5201 0xffff flowid 1:20
"

echo "b02-net ready: bridge=${BRIDGE_IP} gateway+bgserver running, sig=${SIG_BIT}bit/s netem=${NETEM_DELAY}ms mtu=${MTU}"
