#!/bin/bash
# Entrypoint of a delivery-demo container: the logos-delivery-demo UI on a
# virtual display you can watch through noVNC, its delivery node brought into
# the fleet exactly like a delivery-module member's.
#
# The demo never starts a node by itself, so the harness makes the call its
# "Advanced config" row makes -- root.callCreateNodeWithConfig(<profile>) --
# through the QML inspector the standalone host embeds. You see it land in the
# demo's event log, as if the button had been pressed.
#
# The app and its closure come from the nix volume, mounted read-only at /nix;
# their store paths arrive in DEMO_* (from out/demo-paths.json).
set -u
log() { echo "[harness $(hostname)] $*"; }
: "${DEMO_APP:?}" "${DEMO_PLUGIN:?}" "${DEMO_MODULES:?}" "${DEMO_LIBP2P_MODULE:?}"

. /opt/sim/profile.sh

export DISPLAY=:99 QML_INSPECTOR_PORT=${QML_INSPECTOR_PORT:-3768}
Xvfb :99 -screen 0 1440x960x24 >/tmp/xvfb.log 2>&1 &
sleep 1
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &

# The demo's own modules (its declared dependencies, built against the fleet's
# delivery_module) plus libp2p_module, which it does not declare but a node
# configured for plugin discovery cannot start without. Dev variant: the
# standalone host refuses the portable build the members use.
mods=/tmp/modules
mkdir -p "$mods"
cp -rL "$DEMO_MODULES"/. "$mods"/
cp -rL "$DEMO_LIBP2P_MODULE" "$mods"/
chmod -R u+w "$mods"

log "demo starting (profile $MEMBER_CONFIG, ip $IP, libp2p tcp $P2P_PORT, bootstrap ${BOOTSTRAP_ADDR:-none})"
"$DEMO_APP" --modules-dir "$mods" --load libp2p_module --user-dir /data \
  --width 1440 --height 960 "$DEMO_PLUGIN" 2>&1 | sed -u 's/^/[app] /' &
APP=$!
trap 'log "stopping (SIGTERM)"; pkill -TERM -f logos-standalone-app' TERM

ev() { python3 /opt/sim/inspector.py eval "$1"; }
t0=$(date +%s)
if ! python3 /opt/sim/inspector.py wait 'root.backend !== null' "${START_TIMEOUT:-60}"; then
  log "start FAILED: the demo backend never came up"; pkill -f logos-standalone-app; exit 1
fi

# The rendered profile is JSON, and so a valid JS object literal: stringify it
# in the page rather than quote it through two more layers here.
reply=$(ev "root.callCreateNodeWithConfig(JSON.stringify($(cat /data/member.json)))")
case "$reply" in *'"ok": true'*) ;; *)
  log "start FAILED: createNode call refused: $reply"; pkill -f logos-standalone-app; exit 1 ;;
esac
if python3 /opt/sim/inspector.py wait 'root.nodeReady' "${START_TIMEOUT:-60}"; then
  log "start OK ($(( $(date +%s) - t0 ))s)"
else
  log "start FAILED: node not ready: $(ev 'root.lastErrorValue')"; pkill -f logos-standalone-app; exit 1
fi
log "delivery $(ev 'root.deliveryVersionValue')"

reply=$(ev "root.callSubscribe(\"${MESH_CONTENT_TOPIC}\")")
case "$reply" in *'"ok": true'*) log "subscribe ${MESH_CONTENT_TOPIC} OK" ;;
  *) log "subscribe ${MESH_CONTENT_TOPIC} REFUSED: $reply" ;; esac

wait $APP
# A SIGTERM interrupts the first wait; let the app finish shutting down.
wait $APP 2>/dev/null
