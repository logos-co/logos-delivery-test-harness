#!/bin/bash
# Entrypoint of a delivery-module container, bootstrap or member: one logoscore
# daemon hosting delivery_module and the libp2p_module its discovery runs on, the
# delivery node created from the profile the node's group names, started and
# subscribed. Discovery traces go to /traces.
set -u
log() { echo "[harness $(hostname)] $*"; }

. /opt/sim/profile.sh

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
# The RLN modules are optional dependencies too: delivery_module forwards every
# RLN request to liblogos_rln_module, which reads the registry through
# liblogos_lez_rln_module.
RLN_MOD=liblogos_rln_module LEZ_MOD=liblogos_lez_rln_module ALLOC_MOD=rln_gifter_module
# delivery_module 0.4.0 and later: the modules of the split repositories.
[ -d /opt/modules/rln_module ] &&
  RLN_MOD=rln_module LEZ_MOD=rln_lez_module ALLOC_MOD=membership_allocation_client_module
if [ "${RLN:-0}" = 1 ]; then
  retry "load-module $LEZ_MOD" logoscore load-module "$LEZ_MOD"
  # The allocator client: a dependency of newer RLN modules, built only then.
  [ -d "/opt/modules/$ALLOC_MOD" ] &&
    retry "load-module $ALLOC_MOD" logoscore load-module "$ALLOC_MOD"
  retry "load-module $RLN_MOD" logoscore load-module "$RLN_MOD"
fi
retry "load-module delivery_module" logoscore load-module delivery_module
retry "createNode" logoscore call delivery_module createNode @/data/member.json

# delivery_module's RLN identifier, sha256("rln/logos-delivery/v0.0.1").
RLN_IDENTIFIER=5e269b6a19fce081f5808b13442dcbc3522197638dd38df5a28bc4e55236b977
poll() { # <label> <timeout s> <ok regex> <fail regex> <cmd...>: poll a call's reply
  local label=$1 timeout=$2 ok=$3 bad=$4 n=0; shift 4
  while :; do
    "$@" >/tmp/poll.out 2>&1
    if grep -qE "$ok" /tmp/poll.out; then log "$label OK (${n}s)"; return 0; fi
    if grep -qE "$bad" /tmp/poll.out; then
      log "$label FAILED: $(tr '\n' ' ' </tmp/poll.out | cut -c1-400)"; return 1
    fi
    sleep 1; n=$((n + 1))
    if [ "$n" -ge "$timeout" ]; then
      log "$label FAILED: not within ${timeout}s: $(tr '\n' ' ' </tmp/poll.out | cut -c1-400)"; return 1
    fi
  done
}
# RLN comes up on its own thread after createNode. A node sends only with an
# active membership, which its RLN module registers by itself once its LEZ payer
# holds a balance: ask lez-setup for that (/lez/fund), and wait for both.
# RLN_MEMBERSHIP=0 leaves the node without one: it validates, and its sends fail.
if [ "${RLN:-0}" = 1 ]; then
  poll "rlnState Ready" "${RLN_READY_TIMEOUT:-60}" 'Ready' 'Failed' \
    logoscore call delivery_module rlnState || { kill $DAEMON; exit 1; }
  if [ "${RLN_MEMBERSHIP:-1}" = 1 ]; then
    poll "LEZ wallet ready" "${RLN_WALLET_TIMEOUT:-120}" 'state[^a-z]+ready' 'state[^a-z]+failed' \
      logoscore call "$LEZ_MOD" wallet_status || { kill $DAEMON; exit 1; }
    payer=$(grep -oE 'payer[^A-Za-z0-9_]+[A-Za-z0-9]+' /tmp/poll.out | grep -oE '[A-Za-z0-9]+$' | head -1)
    fundto=$payer
    # On the v0.4 chain line a transfer to an account that was never written
    # must present its public key, so that is what lez-setup is asked to fund.
    if [ "${LEZ_VERSION:-0.3}" != 0.3 ]; then
      fundto=$(grep -oE 'payer_public_key[^A-Za-z0-9]+[A-Za-z0-9]+' /tmp/poll.out | grep -oE '[A-Za-z0-9]+$' | head -1)
      [ -n "$fundto" ] || { log "LEZ wallet reports no payer public key"; kill $DAEMON; exit 1; }
    fi
    echo "$fundto" > "/lez/fund/$(hostname).tmp" && mv "/lez/fund/$(hostname).tmp" "/lez/fund/$(hostname)"
    # lez-setup answers in /lez/funded: "ok", or the error.
    funded() { cat "/lez/funded/$(hostname)" 2>/dev/null; }
    poll "LEZ payer $payer funded" "${RLN_FUND_TIMEOUT:-300}" '^ok' '.' funded ||
      { kill $DAEMON; exit 1; }
    poll "RLN membership active" "${RLN_MEMBERSHIP_TIMEOUT:-300}" 'state[^a-z]+active' 'state[^a-z]+failed|refused' \
      logoscore call "$RLN_MOD" get_membership_state \
        "$(cat /lez/registry-id)" "$RLN_IDENTIFIER" || { kill $DAEMON; exit 1; }
    # With a mix registry the node starts without waiting for its Mix
    # membership; this only logs when it becomes active. The identifier is the
    # library's default for mix-rln-identifier-hex.
    if [ "${RLN_MIX_REGISTRY:-0}" = 1 ] && [ "${MIX_RLN:-0}" = 1 ]; then
      poll "Mix RLN membership active" "${RLN_MEMBERSHIP_TIMEOUT:-300}" 'state[^a-z]+active' 'state[^a-z]+failed|refused' \
        logoscore call "$RLN_MOD" get_membership_state "$(cat /lez/mix-registry-id)" \
          6d69782d726c6e2d7370616d2d70726f74656374696f6e2f7631000000000000 &
    fi
  fi
fi

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

# Every delivery_module event of this node, one JSON object per line: what it
# received (messageReceived) and how its own sends went (messagePropagated,
# messageSent, messageError). lib/msg_report.py matches them across nodes.
# Each line gets the local clock in ns ("rx") in front: an event carries its own
# time to the second only, and a reception is timed against the sender's clock.
logoscore watch delivery_module --json 2>/dev/null |
  while IFS= read -r line; do
    printf '{"rx":%s,%s\n' "$(date +%s%N)" "${line#\{}"
  done >>"/traces/$(hostname).msg.jsonl" &

# SEND_COUNT > 0 makes this node a sender: SEND_AFTER seconds from here, one
# message every SEND_INTERVAL seconds on the mesh content topic. The payload,
# "<node>-<n>", is what a receiver's event carries back, so it names the message
# across nodes; the reply to the call holds the request id the sender's own
# events refer to.
send_messages() {
  local i payload reply t
  sleep "${SEND_AFTER:-60}"
  log "sending ${SEND_COUNT} messages, one every ${SEND_INTERVAL:-5}s"
  for i in $(seq 1 "$SEND_COUNT"); do
    payload="$(hostname)-$i"
    # SEND_SIZE pads the payload to that many bytes, to probe a size limit.
    while [ "${#payload}" -lt "${SEND_SIZE:-0}" ]; do payload="$payload-$payload"; done
    [ "${SEND_SIZE:-0}" -gt 0 ] && payload=${payload:0:$SEND_SIZE}
    t=$(date +%s%N)
    reply=$(logoscore call delivery_module send "$MESH_CONTENT_TOPIC" "$payload" --json 2>&1 | tr -d '\n')
    case "$reply" in
      "{"*) ;;
      *) reply="{\"raw\":\"$(echo "$reply" | tr '"\\' "'/" | cut -c1-300)\"}" ;;
    esac
    echo "{\"t\":$t,\"payload\":\"$payload\",\"reply\":$reply}" >>"/traces/$(hostname).send.jsonl"
    sleep "${SEND_INTERVAL:-5}"
  done
  log "sending done"
}
[ "${SEND_COUNT:-0}" -gt 0 ] && send_messages &

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
