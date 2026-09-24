#!/usr/bin/env bash
#:usage
# logos-delivery test harness.
#
#   harness.sh resolve            resolve the module's flake lock into out/resolved.json
#   harness.sh build [--no-image] resolve, nix-build every component, build the image
#   harness.sh gen                generate compose services, Prometheus targets, plan
#   harness.sh up                 gen, then launch each group at its startAfter offset
#   harness.sh down [-v]          tear the stack down
#   harness.sh report [dir] [n]   discovery report over a trace dir (default out/traces),
#                                 n = nodes expected in the DHT (default: from the plan)
#   harness.sh collect [dir]      snapshot a running stack into dir (default
#                                 out/collect/<utc-stamp>): stages, states, traces,
#                                 metrics, report, and the seed's and any troubled
#                                 member's log (COLLECT_LOGS=all keeps every log)
#
# Host requirements: docker, bash, python3. Nix runs only inside the builder
# container, so it is not needed here.
#
# Environment:
#   HARNESS_MANIFEST   manifest to use          (default: ./harness.json)
#   BUILDER_IMAGE      nix builder image        (default: nixos/nix:2.24.9)
#   NIX_VOLUME         builder's nix store      (default: logos-harness-nix)
#   IMAGE              runtime image to build   (default: logos-sim:local)
#   PYTHON             interpreter              (default: python3)
#:end
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

usage() {
  sed -n '/^#:usage$/,/^#:end$/p' "$0" | sed -e '1d;$d' -e 's/^# \{0,1\}//'
  # The per-component prefixes are defined once, in lib/manifest.py. Print them
  # from there rather than keeping a second copy here to drift out of step.
  $PY - <<'PY' 2>/dev/null || true
import manifest
print()
print("Revision overrides -- <PREFIX>_REF (branch, tag or full sha), _REV, _FLAKE:")
for comp, prefix in manifest.ENV_PREFIX.items():
    print(f"  {prefix + '_*':<22} {comp}")
print()
print("Anything left unset comes from the chosen logos-delivery-module's lock;")
print("`harness.sh resolve` shows which is which.")
PY
}
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
  # Traces are a bind mount and the members append to them, so a leftover file
  # would carry the previous run's lookups into this run's report.
  rm -f "$out"/traces/*.trace
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

cmd_report() {
  # Every node the plan launches, the seed included, should end up in the DHT.
  local dir=${1:-$out/traces} n
  n=${2:-$($PY -c "import json;print(sum(len(g['services']) for g in json.load(open('$out/plan.json'))))")}
  $PY "$here/lib/disco_report.py" "$dir" "$n"
}

cmd_collect() {
  local dir=${1:-$out/collect/$(date -u +%Y%m%dT%H%M%SZ)} keep s
  mkdir -p "$dir"
  cmd_gen >/dev/null
  dc ps -a --format '{{.Service}}\t{{.State}}\t{{.Status}}' > "$dir/ps.txt"
  dc logs --no-color 2>/dev/null | sed -n 's/.*\[harness /[harness /p' > "$dir/stages.txt"
  cp "$out"/traces/*.trace "$dir/" 2>/dev/null || true
  cp "$out/run.log" "$out/plan.json" "$out/resolved.json" "$dir/" 2>/dev/null || true
  "$here/promq.sh" up > "$dir/q_up.json" 2>&1 || true
  "$here/promq.sh" logos_delivery_connected_peers_per_shard > "$dir/q_peers.json" 2>&1 || true

  # A container log is a few MB, so by default keep only those with something to
  # explain: the seed, and a member whose trace carries an error, whose stage log
  # says FAILED, REFUSED or lost, or whose container is no longer running.
  if [ "${COLLECT_LOGS:-flagged}" = all ]; then
    keep=$(dc ps -a --format '{{.Service}}')
  else
    keep=$({
      echo seed
      grep -lE 'ERR|UNAVAILABLE|timeout|FAILED' "$out"/traces/*.trace 2>/dev/null |
        sed 's|.*/||; s|\.trace$||'
      sed -nE 's/^\[harness ([^]]+)\] .*(FAILED|REFUSED|module lost).*/\1/p' "$dir/stages.txt"
      awk -F'\t' '$2 != "running" && $1 ~ /^m[0-9]+$/ {print $1}' "$dir/ps.txt"
    } | sort -u)
  fi
  for s in $keep; do dc logs --no-color "$s" > "$dir/$s.log" 2>&1; done
  cmd_report "$dir" > "$dir/report.txt" 2>&1 || true
  log "collected into $dir; logs kept:" $keep
}

case "${1:-}" in
  resolve) shift; cmd_resolve "$@" ;;
  build)   shift; cmd_build "$@" ;;
  gen)     shift; cmd_gen "$@" ;;
  up)      shift; cmd_up "$@" ;;
  down)    shift; cmd_down "$@" ;;
  report)  shift; cmd_report "$@" ;;
  collect) shift; cmd_collect "$@" ;;
  *) usage; exit 1 ;;
esac
