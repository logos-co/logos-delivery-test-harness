#!/bin/bash
# Entrypoint of a delivery-module container, bootstrap or member: one logoscore
# daemon hosting delivery_module and the libp2p_module its discovery runs on, the
# delivery node created from the profile the node's group names, started and
# subscribed. Discovery traces go to /traces.
set -u
log() { echo "[harness $(hostname)] $*"; }

IP=$(hostname -i | awk '{print $1}')
P2P_PORT=${P2P_PORT:-45000}
TCP_PORT=${TCP_PORT:-44000}
METRICS_PORT=${METRICS_PORT:-9100}
MESH_CONTENT_TOPIC=${MESH_CONTENT_TOPIC:-/sim/1/mesh/proto}
LOOKUP=${LOOKUP_INTERVAL:-15}
CLUSTER_ID=${CLUSTER_ID:-42}
NUM_SHARDS=${NUM_SHARDS:-1}
MEMBER_CONFIG=${MEMBER_CONFIG:-member.json.tpl}
# The DHT peer to join through (/ip4/../tcp/../p2p/..). Empty for the first
# bootstrap, which is where the DHT starts: libp2p then takes no bootstrap peer.
BOOTSTRAP_ADDR=${BOOTSTRAP_ADDR:-}

# libp2p_module defaults to /ip4/127.0.0.1/tcp/0; bind the container address so
# the records it publishes are dialable from the other containers.
# A bootstrap also gets a fixed key (LIBP2P_PRIVKEY), so the DHT address its
# members join through is known before it starts.
privkey=""
[ -n "${LIBP2P_PRIVKEY:-}" ] && privkey=",\"privKey\":\"${LIBP2P_PRIVKEY}\""
export LIBP2P_MODULE_CONFIG="{\"addrs\":[\"/ip4/${IP}/tcp/${P2P_PORT}\"],\"transport\":\"tcp\",\"maxConnections\":${P2P_MAX_CONNS:-100},\"maxInConnections\":$(( ${P2P_MAX_CONNS:-100} / 2 )),\"maxOutConnections\":$(( ${P2P_MAX_CONNS:-100} / 2 ))${privkey}}"
export LD_DISCO_TRACE=${LD_DISCO_TRACE:-/traces/$(hostname).trace}
mkdir -p /data "$(dirname "$LD_DISCO_TRACE")"

tpl=/opt/sim/conf/$MEMBER_CONFIG
[ -f "$tpl" ] || { log "no such node profile: $MEMBER_CONFIG"; exit 1; }
bootstraps=""
[ -n "$BOOTSTRAP_ADDR" ] && bootstraps="\"$BOOTSTRAP_ADDR\""
sed -e "s|@IP@|$IP|g" -e "s|@BOOTSTRAPS@|$bootstraps|g" -e "s|@LOOKUP@|$LOOKUP|g" \
    -e "s|@CLUSTER@|$CLUSTER_ID|g" -e "s|@SHARDS@|$NUM_SHARDS|g" \
    -e "s|@TCP_PORT@|$TCP_PORT|g" \
    "$tpl" > /data/member.json

# Spread simultaneous replica starts a little: N daemons loading plugins at the
# same instant trip logos-core's 10 s "plugin never reported loading" timeout.
if [ "${START_JITTER:-0}" -gt 0 ]; then
  sleep $(( $(od -An -N2 -tu2 /dev/urandom) % START_JITTER ))
fi

log "daemon starting (profile $MEMBER_CONFIG, ip $IP, libp2p tcp $P2P_PORT, bootstrap ${BOOTSTRAP_ADDR:-none: this node starts the DHT})"
logoscore daemon -m /opt/modules --persistence-path /data &
DAEMON=$!
WATCHDOG=
# A scheduled stop (stopAfter) is `docker stop`: SIGTERM to this script, PID 1.
# bash ignores it while waiting, so the member would be SIGKILLed 10 s later.
# Hand it to the daemon, which stops its modules and exits -- after silencing
# the watchdog, which would otherwise report the unloading modules as lost.
trap 'log "stopping (SIGTERM)"; [ -n "$WATCHDOG" ] && kill $WATCHDOG; kill -TERM $DAEMON' TERM

n=0
until logoscore list-modules >/dev/null 2>&1; do
  sleep 0.2; n=$((n + 1))
  if [ "$n" -gt 300 ]; then log "daemon never answered"; kill $DAEMON; exit 1; fi
done

retry() { # <label> <cmd...>: three attempts, replies get lost under load
  # The CLI exits 0 even when the module answers with a failure, so the reply
  # body decides: a module error prints as success=false / "error" / "failed".
  local label=$1; shift; local i
  for i in 1 2 3; do
    if "$@" >/tmp/cli.out 2>&1; then
      if grep -qiE '"?success"?[[:space:]]*[:=][[:space:]]*false|^error|failed' /tmp/cli.out; then
        log "$label REFUSED: $(tr '\n' ' ' </tmp/cli.out | cut -c1-300)"
      else
        log "$label OK"; return 0
      fi
    fi
    sleep 1
  done
  log "$label FAILED: $(tr '\n' ' ' </tmp/cli.out | cut -c1-300)"; return 1
}
# libp2p_module is an optional dependency of delivery_module, so logoscore does
# not load it on the module's behalf. The member profile hosts discovery on it
# (plugin-kad-discovery), and a node configured that way refuses to start
# without it.
retry "load-module libp2p_module" logoscore load-module libp2p_module
retry "load-module delivery_module" logoscore load-module delivery_module
retry "createNode" logoscore call delivery_module createNode @/data/member.json

# start only dispatches: the node reports how it went later, through the
# nodeStarted event, and stops itself again if it failed. So "start OK" from the
# call alone says nothing. Watch for the event before dispatching, so it cannot
# fire unseen, and let its outcome decide.
logoscore watch delivery_module --event nodeStarted --json >/tmp/started.json 2>/dev/null &
WATCH=$!
sleep 1
retry "start dispatch" logoscore call delivery_module start
n=0
until grep -q nodeStarted /tmp/started.json 2>/dev/null; do
  sleep 1; n=$((n + 1))
  if [ "$n" -ge "${START_TIMEOUT:-60}" ]; then
    log "start FAILED: no nodeStarted event within ${START_TIMEOUT:-60}s"; kill $WATCH $DAEMON; exit 1
  fi
done
kill $WATCH 2>/dev/null
event=$(grep -m1 nodeStarted /tmp/started.json)
# {"data":{"arg0":<success>,"arg1":<message>,"arg2":<ns>},"event":"nodeStarted",...}
if echo "$event" | grep -qE '"arg0":[[:space:]]*true'; then
  log "start OK (${n}s)"
else
  log "start FAILED: $(echo "$event" | cut -c1-400)"; kill $DAEMON; exit 1
fi

# A node joins a shard's gossipsub topic only when something subscribes: the
# startup subscription in the library runs solely for an app-supplied relay
# handler, and neither a kernel node nor the messaging client registers one.
# So ask for a content topic here. With one shard in the network every content
# topic autoshards onto shard 0, which is the mesh the members share.
retry "subscribe ${MESH_CONTENT_TOPIC}" \
  logoscore call delivery_module subscribe "${MESH_CONTENT_TOPIC}"

# /metrics for this member: the openmetrics module merges the delivery library's
# Prometheus registry (rendered text) with nim-libp2p's registry inside
# libp2p_module (structured series), labelling each series with its module.
retry "load-module openmetrics" logoscore load-module openmetrics
retry "openmetrics start" logoscore call openmetrics start \
  "{\"port\":${METRICS_PORT},\"modules\":[{\"name\":\"delivery_module\",\"format\":\"text\"},{\"name\":\"libp2p_module\",\"format\":\"data\"}]}"

# A module host can die mid-run while the daemon -- and so the container -- lives
# on: logoscore then lists the module as not_loaded and every call into it fails,
# which nothing else reports. Poll, and say so once per module, with the time
# and the module hosts still running.
watch_modules() {
  local lost=" " m st hosts
  while sleep "${WATCHDOG_INTERVAL:-5}"; do
    st=$(logoscore status --json 2>/dev/null) || continue
    # Only a whole answer counts: a reply cut short under load lists no module.
    echo "$st" | grep -q '"daemon":{[^}]*"status":"running"' || continue
    for m in libp2p_module delivery_module openmetrics; do
      case "$lost" in *" $m "*) continue ;; esac
      echo "$st" | grep -q "\"name\":\"$m\",\"status\":\"loaded\"" && continue
      lost="$lost$m "
      hosts=$(ps -eo args | sed -n 's/^[^ ]*logos_host[^ ]* .*--name \([^ ]*\).*/\1/p' | tr '\n' ' ')
      log "module lost: $m at $(date -u +%H:%M:%S)" \
        "($(echo "$st" | grep -o "\"name\":\"$m\",\"status\":\"[a-z_]*\""); hosts left: $hosts)"
    done
  done
}
watch_modules &
WATCHDOG=$!

wait $DAEMON
# A SIGTERM interrupts the first wait; let the daemon finish shutting down.
wait $DAEMON 2>/dev/null
