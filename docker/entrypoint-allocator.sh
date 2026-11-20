#!/bin/bash
# Entrypoint of an rln-allocator container: a logoscore daemon hosting
# libp2p_module (kad-dht plus service discovery), the LEZ registry provider and
# membership_allocation_module, which registers RLN memberships for the nodes
# that ask it, from this node's own LEZ payer. lez-setup funds that payer.
set -u
log() { echo "[harness $(hostname)] $*"; }

IP=$(hostname -i | awk '{print $1}')
P2P_PORT=${P2P_PORT:-45000}
METRICS_PORT=${METRICS_PORT:-9100}
BOOTSTRAP_ADDR=${BOOTSTRAP_ADDR:-}
# What the payer is funded with: every registration of the run is paid from it.
ALLOCATOR_FUNDING=${ALLOCATOR_FUNDING:-200000000000}

# BOOTSTRAP_ADDR: comma-separated /ip4/../tcp/../p2p/<id> -> libp2p bootstrapNodes.
bn=""
IFS=, read -ra boots <<<"$BOOTSTRAP_ADDR"
for a in "${boots[@]}"; do
  [ -z "$a" ] && continue
  bn="$bn${bn:+,}{\"peerId\":\"${a##*/p2p/}\",\"addrs\":[\"${a%/p2p/*}\"]}"
done
privkey=""
[ -n "${LIBP2P_PRIVKEY:-}" ] && privkey=",\"privKey\":\"${LIBP2P_PRIVKEY}\""
conns=${P2P_MAX_CONNS:-100}
export LIBP2P_MODULE_CONFIG="{\"addrs\":[\"/ip4/${IP}/tcp/${P2P_PORT}\"],\"transport\":\"tcp\",\"maxConnections\":${conns},\"maxInConnections\":$((conns / 2)),\"maxOutConnections\":$((conns / 2)),\"mountGossipsub\":false,\"mountKad\":true,\"mountServiceDiscovery\":true,\"bootstrapNodes\":[${bn}]${privkey}}"
mkdir -p /data /traces

# The registries to serve, as lez-setup published them (logos:local:<hex>):
# the allocator names a registry by its config account, the hex part.
n=0
until [ -s /lez/registry-id ]; do
  sleep 1; n=$((n + 1))
  if [ "$n" -ge "${RLN_SETUP_TIMEOUT:-600}" ]; then
    log "allocator FAILED: no registry from lez-setup within ${RLN_SETUP_TIMEOUT:-600}s"; exit 1
  fi
done
registry=$(cat /lez/registry-id); registry=${registry##*:}
registries=""
if [ "${RLN_MIX_REGISTRY:-0}" = 1 ]; then
  [ -s /lez/mix-registry-id ] || { log "allocator FAILED: no mix registry from lez-setup"; exit 1; }
  mix=$(cat /lez/mix-registry-id)
  registries=",\"registries\":[\"${mix##*:}\"]"
fi

log "allocator starting (ip $IP, libp2p tcp $P2P_PORT, bootstrap ${BOOTSTRAP_ADDR:-none})"
logoscore daemon -m /opt/modules --persistence-path /data &
DAEMON=$!
trap 'log "stopping (SIGTERM)"; kill -TERM $DAEMON' TERM

n=0
until logoscore list-modules >/dev/null 2>&1; do
  sleep 0.2; n=$((n + 1))
  if [ "$n" -gt 300 ]; then log "daemon never answered"; kill $DAEMON; exit 1; fi
done

ok() { # the CLI exits 0 on a module failure too: the reply decides
  ! grep -qiE '"?success"?[[:space:]]*[:=][[:space:]]*false|^error|"error"|failed' "$1"
}
retry() { # <label> <cmd...>: idempotent calls only
  local label=$1; shift; local i
  for i in 1 2 3; do
    if "$@" >/tmp/cli.out 2>&1 && ok /tmp/cli.out; then log "$label OK"; return 0; fi
    sleep 1
  done
  log "$label FAILED: $(tr '\n' ' ' </tmp/cli.out | cut -c1-300)"; return 1
}
die() { kill $DAEMON; exit 1; }

retry "load-module libp2p_module" logoscore load-module libp2p_module || die
retry "load-module rln_lez_module" logoscore load-module rln_lez_module || die
retry "load-module membership_allocation_module" logoscore load-module membership_allocation_module || die

# start exactly once (see entrypoint-agent.sh): readiness is judged by
# getNodeInfo, not by the reply, which may time out while the DHT bootstraps.
t0=$(date +%s)
logoscore call libp2p_module start >/tmp/start.out 2>&1
log "libp2p start reply: $(tr '\n' ' ' </tmp/start.out | cut -c1-200)"
until logoscore call libp2p_module getNodeInfo PeerId --json >/tmp/pid.out 2>&1 && grep -q '"success":true' /tmp/pid.out; do
  if [ $(( $(date +%s) - t0 )) -ge "${START_TIMEOUT:-180}" ]; then
    log "start FAILED: libp2p not up after ${START_TIMEOUT:-180}s: $(tr '\n' ' ' </tmp/pid.out | cut -c1-300)"; die
  fi
  sleep 2
done
log "start OK ($(( $(date +%s) - t0 ))s) $(grep -oE '(16Uiu2|12D3KooW)[A-Za-z0-9]*' /tmp/pid.out | head -1)"
logoscore call libp2p_module discoStart >/tmp/disco.out 2>&1
log "discoStart reply: $(tr '\n' ' ' </tmp/disco.out | cut -c1-200)"

# The payer every registration is paid from: wait for the wallet, ask
# lez-setup for the funds (by public key, see entrypoint-module.sh).
n=0
until logoscore call rln_lez_module wallet_status >/tmp/wallet.out 2>&1 && grep -qE 'state[^a-z]+ready' /tmp/wallet.out; do
  if grep -qE 'state[^a-z]+failed' /tmp/wallet.out || [ "$n" -ge "${RLN_WALLET_TIMEOUT:-120}" ]; then
    log "LEZ wallet FAILED: $(tr '\n' ' ' </tmp/wallet.out | cut -c1-400)"; die
  fi
  sleep 1; n=$((n + 1))
done
payer=$(grep -oE 'payer[^A-Za-z0-9_]+[A-Za-z0-9]+' /tmp/wallet.out | grep -oE '[A-Za-z0-9]+$' | head -1)
payerkey=$(grep -oE 'payer_public_key[^A-Za-z0-9]+[A-Za-z0-9]+' /tmp/wallet.out | grep -oE '[A-Za-z0-9]+$' | head -1)
[ -n "$payerkey" ] || { log "LEZ wallet reports no payer public key"; die; }
log "LEZ wallet ready (${n}s), payer $payer"
echo "$payerkey $ALLOCATOR_FUNDING" > "/lez/fund/$(hostname).tmp" && mv "/lez/fund/$(hostname).tmp" "/lez/fund/$(hostname)"
n=0
until [ -s "/lez/funded/$(hostname)" ]; do
  sleep 1; n=$((n + 1))
  if [ "$n" -ge "${RLN_FUND_TIMEOUT:-300}" ]; then log "LEZ payer FAILED: not funded within ${RLN_FUND_TIMEOUT:-300}s"; die; fi
done
grep -q '^ok' "/lez/funded/$(hostname)" || { log "LEZ payer FAILED: $(cut -c1-300 "/lez/funded/$(hostname)")"; die; }
log "LEZ payer funded with $ALLOCATOR_FUNDING (${n}s)"

# serve once: mounts the protocol and advertises the (registry, auth type)
# pairs through service discovery. peer-any: any libp2p peer, one membership
# per peer per registry.
args="{\"config\":\"$registry\"$registries,\"authVerifiers\":{\"peer-any\":{}}${ALLOCATOR_SERVE_ARGS:+,$ALLOCATOR_SERVE_ARGS}}"
logoscore call membership_allocation_module serve "$args" >/tmp/serve.out 2>&1
if grep -q 'mounted[^a-z]*true' /tmp/serve.out; then
  log "serve OK: $(tr '\n' ' ' </tmp/serve.out | cut -c1-600)"
else
  log "serve FAILED: $(tr '\n' ' ' </tmp/serve.out | cut -c1-600)"; die
fi

retry "load-module openmetrics" logoscore load-module openmetrics
retry "openmetrics start" logoscore call openmetrics start \
  "{\"port\":${METRICS_PORT},\"modules\":[{\"name\":\"libp2p_module\",\"format\":\"data\"}]}"

# What the payer holds, once a minute: it falls with every registration paid.
while sleep 60; do
  log "payer balance: $(logoscore call rln_lez_module get_native_balance "" 2>&1 | grep -oE 'balance[^0-9]+[0-9]+' | grep -oE '[0-9]+$')"
done &

wait $DAEMON
wait $DAEMON 2>/dev/null
