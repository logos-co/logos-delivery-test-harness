# Sourced by every entrypoint that runs delivery_module (delivery-module and
# delivery-demo nodes): the node's addresses, its libp2p_module config, its
# discovery trace, and the node config rendered from the group's profile into
# /data/member.json. One copy, so a demo node joins exactly like a module member.
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
