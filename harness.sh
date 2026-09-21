#!/usr/bin/env bash
# logos-delivery test harness.
#
#   harness.sh resolve            resolve the module's flake lock into out/resolved.json
#   harness.sh build [--no-image] resolve, nix-build every component, build the image
#   harness.sh gen                generate compose services, Prometheus targets, plan
#   harness.sh up                 gen, then launch each group at its startAfter offset
#   harness.sh down [-v]          tear the stack down
#   harness.sh report             discovery report over the collected traces
#
# Host requirements: docker, bash, python3. Nix runs only inside the builder
# container, so it is not needed here.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST=${HARNESS_MANIFEST:-$here/harness.json}
BUILDER_IMAGE=${BUILDER_IMAGE:-nixos/nix:2.24.9}
NIX_VOLUME=${NIX_VOLUME:-logos-harness-nix}
IMAGE=${IMAGE:-logos-sim:local}
PY=${PYTHON:-python3}

out=$here/out
mkdir -p "$out/traces"
export PYTHONPATH="$here/lib"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
dc() {
  docker compose -f "$here/docker-compose.yml" -f "$out/compose.groups.yml" "$@"
}

cmd_resolve() {
  local flake
  flake=$($PY -c "import manifest;print(manifest.module_flake(manifest.load('$MANIFEST')))")
  log "reading the flake lock of $flake"
  docker run --rm -v "$NIX_VOLUME:/nix" -v "$out:/out" "$BUILDER_IMAGE" sh -euc "
    mkdir -p /etc/nix
    echo 'experimental-features = nix-command flakes' > /etc/nix/nix.conf
    # --no-write-lock-file: a remote flake whose own lock is not fully pinned
    # would otherwise be updated in place, which nix cannot do and we do not want.
    nix flake metadata --json --no-write-lock-file '$flake' > /out/lock.json
  "
  $PY "$here/lib/resolve.py" "$MANIFEST" "$out/lock.json" "$out/resolved.json"
}

cmd_build() {
  cmd_resolve
  $PY "$here/lib/gen_buildscript.py" "$out/resolved.json" "$out/build-inside.sh"
  log "nix builds in $BUILDER_IMAGE (store volume $NIX_VOLUME)"
  docker run --rm \
    -v "$NIX_VOLUME:/nix" \
    -v "$out:/out" \
    "$BUILDER_IMAGE" sh -euc 'sh /out/build-inside.sh'

  if [ "${1:-}" = "--no-image" ]; then
    log "skipping the image build"
    return
  fi
  log "docker build $IMAGE"
  docker build -t "$IMAGE" -f "$here/docker/Dockerfile" "$here"
}

cmd_gen() {
  $PY "$here/lib/gen_compose.py" "$MANIFEST" \
    "$out/compose.groups.yml" "$out/prom-targets.json" "$out/plan.json"
}

cmd_up() {
  cmd_gen
  : > "$out/run.log"
  runlog() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$out/run.log"; }

  runlog "manifest $MANIFEST"
  [ -f "$out/resolved.json" ] && runlog "module $($PY -c "
import json;print(json.load(open('$out/resolved.json'))['module'])")"

  runlog "starting monitoring"
  dc up -d prometheus grafana >/dev/null

  # Groups at offset 0 go first; the seed among them gates the rest, because a
  # member cannot bootstrap before it answers.
  local zero
  zero=$($PY -c "
import json
plan=json.load(open('$out/plan.json'))
print(' '.join(s for g in plan if g['startAfter']==0 for s in g['services']))")
  runlog "T0 -- launching: $zero"
  local t0
  t0=$(date +%s)
  dc up -d $zero >/dev/null

  # Every later group, at its own offset from T0.
  $PY -c "
import json
plan=json.load(open('$out/plan.json'))
for g in sorted(plan, key=lambda g: g['startAfter']):
    if g['startAfter'] > 0:
        print(g['startAfter'], g['name'], ' '.join(g['services']))" |
    while read -r after name services; do
      while [ $(($(date +%s) - t0)) -lt "$after" ]; do sleep 1; done
      runlog "T0+$(($(date +%s) - t0))s -- launching $name: $services"
      dc up -d $services >/dev/null
    done
  runlog "all groups launched"
}

cmd_down() { cmd_gen >/dev/null; dc down "$@"; }

cmd_report() { $PY "$here/lib/disco_report.py" "$out/traces" "$@"; }

case "${1:-}" in
  resolve) shift; cmd_resolve "$@" ;;
  build)   shift; cmd_build "$@" ;;
  gen)     shift; cmd_gen "$@" ;;
  up)      shift; cmd_up "$@" ;;
  down)    shift; cmd_down "$@" ;;
  report)  shift; cmd_report "$@" ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
