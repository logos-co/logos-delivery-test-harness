#!/usr/bin/env bash
#:usage
# logos-delivery test harness.
#
#   harness.sh resolve            resolve the module's flake lock into out/resolved.json
#   harness.sh build [--no-image] resolve, nix-build every component, build the image
#   harness.sh gen                generate compose services, Prometheus targets, plan
#   harness.sh up                 gen, then launch each group at its startAfter offset
#                                 and stop it at its stopAfter; blocks until done
#   harness.sh run [sec] [dir]    up, run for <sec> from T0 (default: the manifest's
#                                 "duration"), collect into dir, down
#   harness.sh suite [--build] <manifest>...
#                                 run each manifest for its "duration", one after
#                                 another, into out/collect/suite-<utc-stamp>/<name>,
#                                 and print each one's verdict; --build first builds
#                                 the images that are missing
#   harness.sh start <group|node>...  start a group (e.g. a manual one) or node
#                                 in the running stack; stop does the reverse
#   harness.sh down [-v]          tear the stack down
#   harness.sh report [dir] [n]   discovery report over a trace dir (default out/traces),
#                                 n = nodes expected in the DHT (default: from the plan)
#   harness.sh collect [dir]      snapshot a running stack into dir (default
#                                 out/collect/<utc-stamp>): stages, states, traces,
#                                 metrics, report, and the bootstraps' and any troubled
#                                 member's log (COLLECT_LOGS=all keeps every log)
#
# Host requirements: docker, bash, python3. Nix runs only inside the builder
# container, so it is not needed here.
#
# Environment:
#   HARNESS_MANIFEST   manifest to use          (default: ./harness.json)
#   BUILDER_IMAGE      nix builder image        (default: nixos/nix:2.24.9)
#   NIX_VOLUME         builder's nix store      (default: logos-harness-nix)
#   IMAGE              runtime image to build and run, over the manifest's
#                      "image"                  (default: logos-sim:local)
#   LEZ_REF            logos-execution-zone tag the RLN sequencer is built from
#                                               (default: v0.3.2)
#   PYTHON             interpreter              (default: python3)
#:end
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST=${HARNESS_MANIFEST:-$here/harness.json}
BUILDER_IMAGE=${BUILDER_IMAGE:-nixos/nix:2.24.9}
NIX_VOLUME=${NIX_VOLUME:-logos-harness-nix}
LEZ_REF=${LEZ_REF:-v0.3.2}
PY=${PYTHON:-python3}

out=$here/out
mkdir -p "$out/traces"
export PYTHONPATH="$here/lib"

# The manifest names its image; IMAGE in the environment wins (lib/manifest.py).
image() { $PY -c "import manifest;print(manifest.image(manifest.load('${1:-$MANIFEST}')))"; }

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
runlog() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$out/run.log"; }

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
  local demo image
  image=$(image)
  demo=$($PY -c "import json;print('logos-delivery-demo' in json.load(open('$out/resolved.json'))['components'])")
  [ "$demo" = True ] && $PY "$here/lib/demo_paths.py" "$out"

  [ "$($PY -c "import manifest;print(manifest.uses_rln(manifest.load('$MANIFEST')))")" = True ] && build_lez

  if [ "${1:-}" = "--no-image" ]; then
    log "skipping the image build"
    return
  fi
  log "docker build $image"
  docker build -t "$image" -f "$here/docker/Dockerfile" "$here"
  if [ "$demo" = True ]; then
    log "docker build logos-sim-demo:local"
    docker build -t logos-sim-demo:local --build-arg BASE="$image" \
      -f "$here/docker/Dockerfile.demo" "$here"
  fi
}

# What an RLN run adds to the nix builds: the local LEZ chain. Each piece is
# built once and kept; remove the image (or out/lez-tools) to build it again.
build_lez() {
  local seq
  seq=$($PY -c "import manifest;print(manifest.load('$MANIFEST')['rln']['sequencerImage'])")
  # The standalone sequencer, from source for this host's platform (see
  # docker/Dockerfile.lez-sequencer). A manifest naming another image brings its own.
  if [ "$seq" = logos-sim-lez-sequencer:local ] && ! docker image inspect "$seq" >/dev/null 2>&1; then
    log "docker build $seq from logos-execution-zone $LEZ_REF"
    [ -d "$out/lez-src/logos-execution-zone/.git" ] ||
      git clone -q --depth 1 --branch "$LEZ_REF" \
        https://github.com/logos-blockchain/logos-execution-zone.git "$out/lez-src/logos-execution-zone"
    docker build -t "$seq" -f "$here/docker/Dockerfile.lez-sequencer" "$out/lez-src/logos-execution-zone"
  fi
  # logos-lez-rln's guest programs and tools (docker/build-lez.sh): the guest
  # programs as linux/amd64, with the docker socket; the tools natively.
  if [ ! -x "$out/lez-tools/lez-rln/target/release/run_setup" ]; then
    local platform
    for platform in linux/amd64 ""; do
      log "building logos-lez-rln's $([ -n "$platform" ] && echo "guest programs ($platform)" || echo tools)"
      docker build -q ${platform:+--platform $platform} -t logos-sim-lez-build:local \
        -f "$here/docker/Dockerfile.lez-build" "$here/docker"
      docker run --rm ${platform:+--platform $platform} \
        -v logos-harness-lez-build:/work -v "$out:/out" \
        -v "$here/docker/build-lez.sh:/build-lez.sh:ro" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        logos-sim-lez-build:local bash /build-lez.sh "$([ -n "$platform" ] && echo guest || echo tools)"
    done
  fi
  log "docker build logos-sim-lez:local"
  docker build -q -t logos-sim-lez:local -f "$here/docker/Dockerfile.lez" "$here"
}

cmd_gen() {
  $PY "$here/lib/gen_compose.py" "$MANIFEST" \
    "$out/compose.groups.yml" "$out/prom-targets.json" "$out/plan.json"
}

cmd_up() {
  cmd_gen
  local img
  for img in $(sed -n 's/^    image: //p' "$out/compose.groups.yml" | grep '^logos-sim' | sort -u); do
    docker image inspect "$img" >/dev/null 2>&1 ||
      { echo "harness: image $img is not built -- HARNESS_MANIFEST=$MANIFEST ./harness.sh build" >&2; exit 1; }
  done
  # Traces are a bind mount and the members append to them, so a leftover file
  # would carry the previous run's lookups into this run's report.
  rm -f "$out"/traces/*.trace "$out"/traces/*.msg.jsonl "$out"/traces/*.send.jsonl "$out"/traces/*.sd.jsonl
  : > "$out/run.log"

  runlog "manifest $MANIFEST"
  [ -f "$out/resolved.json" ] && runlog "module $($PY -c "
import json;print(json.load(open('$out/resolved.json'))['module'])")"

  runlog "starting monitoring"
  dc up -d prometheus grafana >/dev/null
  # An RLN manifest: the local LEZ chain, from a clean out/lez. Nodes with RLN=1
  # wait for the registry it deploys, so it is not part of the schedule.
  if grep -q '^  lez-setup:' "$out/compose.groups.yml"; then
    rm -rf "$out/lez"; mkdir -p "$out/lez/pub"
    runlog "starting the LEZ chain (lez-init, lez-sequencer, lez-setup)"
    dc up -d lez-setup >/dev/null
  fi

  # Groups at offset 0 go first; the bootstraps among them gate the rest,
  # because a member cannot join before its bootstrap answers.
  local zero t0
  zero=$($PY -c "
import json
plan=json.load(open('$out/plan.json'))
print(' '.join(s for g in plan if g['startAfter']==0 and not g.get('manual') for s in g['services']))")
  $PY -c "
import json
for g in json.load(open('$out/plan.json')):
    when = 'manual: harness.sh start ' + g['name'] if g.get('manual') else f'from T0+{g[\"startAfter\"]}s'
    for n, p in g.get('vnc', {}).items():
        print(f'{n}: http://localhost:{p}/vnc.html  ({g[\"name\"]}, {when})')" |
    while read -r line; do runlog "noVNC $line"; done
  runlog "T0 -- launching: $zero"
  t0=$(date +%s)
  echo "$t0" > "$out/t0"
  dc up -d $zero >/dev/null

  # Every later launch and every stop, in time order. --no-deps: a bootstrap that
  # was stopped on schedule must stay stopped when a late member arrives.
  # `run` ends the schedule early by creating out/abort.
  $PY -c "
import json
plan=json.load(open('$out/plan.json'))
ev=[(g['startAfter'],1,'launching',g) for g in plan if g['startAfter']>0 and not g.get('manual')]
ev+=[(g['stopAfter'],0,'stopping',g) for g in plan if 'stopAfter' in g]
for at,_,what,g in sorted(ev, key=lambda e: e[:2]):
    print(at, what, g['name'], ' '.join(g['services']))" |
    while read -r at what name services; do
      while [ $(($(date +%s) - t0)) -lt "$at" ]; do
        [ -e "$out/abort" ] && break 2
        sleep 1
      done
      runlog "T0+$(($(date +%s) - t0))s -- $what $name: $services"
      if [ "$what" = launching ]; then
        dc up -d --no-deps $services >/dev/null
      else
        dc stop $services >/dev/null 2>&1
      fi
    done
  if [ -e "$out/abort" ]; then
    runlog "schedule cut short by the end of the run"
  else
    runlog "schedule complete"
  fi
}

cmd_run() {
  # up, wait until T0 + <seconds>, collect, down. Launches or stops scheduled
  # after the end of the run are dropped, and said so.
  local duration dir=${2:-} up t0 late
  duration=${1:-$($PY -c "import manifest;print(manifest.load('$MANIFEST').get('duration', ''))")}
  case "$duration" in ''|*[!0-9]*)
    echo "usage: harness.sh run <seconds> [collect-dir]  (seconds may be left out when the manifest has a \"duration\")" >&2
    exit 1 ;;
  esac
  rm -f "$out/t0" "$out/abort"
  cmd_gen >/dev/null
  late=$($PY -c "
import json
plan=json.load(open('$out/plan.json'))
print(' '.join(f\"{g['name']}@{t}s\" for g in plan if not g.get('manual') for t in (g['startAfter'], g.get('stopAfter', 0)) if t >= $duration))")
  [ -n "$late" ] && log "warning: scheduled at or after the ${duration}s end, so dropped: $late"

  cmd_up & up=$!
  until [ -s "$out/t0" ]; do
    kill -0 $up 2>/dev/null || { log "up failed before T0"; return 1; }
    sleep 1
  done
  t0=$(cat "$out/t0")
  # CPU and memory of every container, every 30 s: out/stats.tsv.
  : > "$out/stats.tsv"
  local tick=0
  while [ $(($(date +%s) - t0)) -lt "$duration" ]; do
    sleep 5; tick=$((tick + 1))
    [ $((tick % 6)) -eq 0 ] && docker stats --no-stream \
      --format "$(($(date +%s) - t0))\t{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" 2>/dev/null |
      grep "$(basename "$here")-" >> "$out/stats.tsv"
  done
  touch "$out/abort"
  wait $up
  rm -f "$out/abort"
  log "T0+$(($(date +%s) - t0))s -- end of run"
  cmd_collect "$dir"
  cmd_down -v >/dev/null 2>&1
  log "stack down"
}

# --remove-orphans: the compose file is regenerated from the current manifest,
# so services of a stack started with another one would otherwise survive.
cmd_manual() {
  # start|stop <group|node>...: by hand, in the running stack. For manual
  # groups -- which up and run never launch -- and equally to add or take down
  # any group or node mid-run. Works on the compose file the stack was brought
  # up with; nothing is regenerated.
  local what=$1; shift
  [ $# -gt 0 ] || { echo "usage: harness.sh $what <group|node>..." >&2; exit 1; }
  if [ ! -f "$out/plan.json" ] || [ -z "$(dc ps -q prometheus 2>/dev/null)" ]; then
    echo "harness: no stack is up -- ./harness.sh up first" >&2; exit 1
  fi
  local info services needs vnc b at reply
  info=$($PY - "$out/plan.json" "$@" <<'PY'
import json, sys
plan, names = json.load(open(sys.argv[1])), sys.argv[2:]
node_group = {s: g for g in plan for s in g["services"]}
services = []
for n in names:
    group = next((g for g in plan if g["name"] == n), None)
    if group:
        services += group["services"]
    elif n in node_group:
        services.append(n)
    else:
        sys.exit(f"harness: no group or node named '{n}' in this plan")
services = list(dict.fromkeys(services))
needs = {node_group[s].get("joins", {}).get(s) for s in services} - {None} - set(services)
vnc = [f"{s}: http://localhost:{node_group[s]['vnc'][s]}/vnc.html"
       for s in services if s in node_group[s].get("vnc", {})]
print(" ".join(services)); print(" ".join(sorted(needs))); print("|".join(vnc))
PY
  ) || exit 1
  services=$(sed -n 1p <<<"$info"); needs=$(sed -n 2p <<<"$info"); vnc=$(sed -n 3p <<<"$info")
  at="T0+?"
  [ -s "$out/t0" ] && at="T0+$(( $(date +%s) - $(cat "$out/t0") ))s"
  if [ "$what" = start ]; then
    for b in $needs; do
      dc ps --status running --services | grep -qx "$b" ||
        { echo "harness: '$b', the bootstrap they join through, is not running" >&2; exit 1; }
    done
    runlog "$at -- starting by hand: $services"
    # compose narrates every container on stderr; show it only on failure.
    if ! reply=$(dc up -d --no-deps $services 2>&1); then echo "$reply" >&2; exit 1; fi
    [ -n "$vnc" ] && tr '|' '\n' <<<"$vnc" | while read -r line; do runlog "noVNC $line"; done
  else
    runlog "$at -- stopping by hand: $services"
    if ! reply=$(dc stop $services 2>&1); then echo "$reply" >&2; exit 1; fi
  fi
  return 0
}

cmd_down() { cmd_gen >/dev/null; dc down --remove-orphans "$@"; }

cmd_suite() {
  # One stack per host, so one manifest after another; each runs for its own
  # "duration" and is judged by its delivery report.
  local build=0 m name dir root verdict rc=0
  [ "${1:-}" = "--build" ] && { build=1; shift; }
  [ $# -gt 0 ] || { echo "usage: harness.sh suite [--build] <manifest>..." >&2; exit 1; }
  for m in "$@"; do
    $PY -c "
import manifest, sys
'duration' in manifest.load('$m') or sys.exit('harness: $m has no \"duration\" (seconds), which suite needs')" || exit 1
  done
  root=$out/collect/suite-$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$root"
  for m in "$@"; do
    name=$(basename "$m" .json); dir=$root/$name
    if [ "$build" = 1 ] && ! docker image inspect "$(image "$m")" >/dev/null 2>&1; then
      log "suite: $name -- building $(image "$m")"
      HARNESS_MANIFEST=$m "$0" build > "$root/$name.build.log" 2>&1 ||
        log "suite: $name -- build failed, see $root/$name.build.log"
    fi
    log "suite: $name -- running"
    if HARNESS_MANIFEST=$m "$0" run "" "$dir" > "$root/$name.log" 2>&1; then
      verdict=$(sed -n 's/^verdict: //p' "$dir/delivery.txt" 2>/dev/null | tail -1) || true
      if [ -z "$verdict" ]; then
        # A manifest with a sender must come back with a verdict.
        if grep -q '"SEND_COUNT"' "$m"; then verdict="NO REPORT: senders but no verdict in delivery.txt"
        else verdict="no traffic to judge"; fi
      fi
    else
      verdict="RUN FAILED: $(tail -1 "$root/$name.log" | cut -c1-120)"
    fi
    case "$verdict" in PASS*|no\ traffic*) ;; *) rc=1 ;; esac
    printf '%-44s %s\n' "$name" "$verdict" | tee -a "$root/summary.txt"
  done
  log "suite: results in $root"
  return $rc
}

cmd_report() {
  # Every node the plan launches, bootstraps included, should end up in the DHT.
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
  cp "$out"/traces/*.msg.jsonl "$out"/traces/*.send.jsonl "$dir/" 2>/dev/null || true
  cp "$out"/traces/*.sd.jsonl "$dir/" 2>/dev/null || true  # sd-agent event logs
  cp "$out/run.log" "$out/plan.json" "$out/resolved.json" "$out/t0" "$dir/" 2>/dev/null || true
  "$here/promq.sh" up > "$dir/q_up.json" 2>&1 || true
  "$here/promq.sh" logos_delivery_connected_peers_per_shard > "$dir/q_peers.json" 2>&1 || true
  # Relay connections per node, both directions: the one peer figure both kinds
  # export (a logosdeliverynode has no per-shard gauge).
  "$here/promq.sh" 'sum by (node, role, kind) (logos_delivery_connected_peers{protocol="/vac/waku/relay/2.0.0"})' \
    > "$dir/q_relay.json" 2>&1 || true
  # Usable mix pool per mix-mounted node, every scrape since T0: when discovery
  # had handed a sender enough mix nodes to build a path, and when a stopped
  # mix node left the pool.
  local window=1800
  [ -s "$out/t0" ] && window=$(( $(date +%s) - $(cat "$out/t0") + 60 ))
  "$here/promq.sh" "mix_pool_size[${window}s]" > "$dir/q_mixpool.json" 2>&1 || true
  # What each mix node handled: received, forwarded, failed, reply timeouts.
  "$here/promq.sh" '{__name__=~"mix_messages_recvd_total|mix_messages_forwarded_total|mix_messages_error_total|mix_reply_timeouts_total"}' \
    > "$dir/q_mixload.json" 2>&1 || true
  cp "$out/stats.tsv" "$dir/" 2>/dev/null || true
  # Mix routes, from every node's log: which node was the exit of which message
  # (its lightpush request names the hash), and each try of a sender.
  # A node whose log comes back empty or with an error is asked again, and
  # named in warnings.txt if it stays that way: its routes are then missing.
  local try
  : > "$dir/mixlog.txt"; : > "$dir/warnings.txt"
  for s in $(dc ps -a --format '{{.Service}}' | grep -v -e '^lez-' -e prometheus -e grafana); do
    for try in 1 2 3; do
      if dc logs --no-color "$s" 2>"$dir/.logs.err" | $PY -c "
import re, sys
ansi = re.compile(r'\x1b\[[0-9;]*m')
n = 0
for line in sys.stdin.buffer:
    n += 1
    if b'Handling lightpush request' in line:
        m = re.search(r'msg_hash=(0x[0-9a-f]+)', ansi.sub('', line.decode(errors='replace')))
        if m: print('$s exit', m.group(1))
    elif b'Trying message delivery via Mix' in line:
        m = re.search(r'msgHash=(0x[0-9a-f]+) tryCount=(\d+)', line.decode(errors='replace'))
        if m: print('$s try', m.group(1), m.group(2))
sys.exit(0 if n else 1)
" > "$dir/.mixlog.part"; then
        cat "$dir/.mixlog.part" >> "$dir/mixlog.txt"
        break
      fi
      [ "$try" = 3 ] && echo "no log from $s for the route data: $(tr '\n' ' ' < "$dir/.logs.err" | cut -c1-200)" >> "$dir/warnings.txt"
      sleep 2
    done
  done
  rm -f "$dir/.mixlog.part" "$dir/.logs.err"
  if [ -s "$dir/warnings.txt" ]; then
    log "warning: $(cat "$dir/warnings.txt")"
  else
    rm -f "$dir/warnings.txt"
  fi

  # A container log is a few MB, so by default keep only those with something to
  # explain: every bootstrap, every logosdeliverynode (no trace to judge them
  # by), every delivery-demo (the app's own log), and a module member whose
  # trace carries an error, whose stage log
  # says FAILED, REFUSED or lost, or whose container is no longer running.
  if [ "${COLLECT_LOGS:-flagged}" = all ]; then
    keep=$(dc ps -a --format '{{.Service}}')
  else
    keep=$({
      $PY -c "
import json
print('\\n'.join(s for g in json.load(open('$out/plan.json')) if g['role']=='bootstrap' or g['kind'] in ('logosdeliverynode','delivery-demo') for s in g['services']))"
      grep -lE 'ERR|UNAVAILABLE|timeout|FAILED' "$out"/traces/*.trace 2>/dev/null |
        sed 's|.*/||; s|\.trace$||'
      sed -nE 's/^\[harness ([^]]+)\] .*(FAILED|REFUSED|module lost).*/\1/p' "$dir/stages.txt"
      awk -F'\t' '$2 != "running" && $1 ~ /^m[0-9]+$/ {print $1}' "$dir/ps.txt"
    } | sort -u)
  fi
  for s in $keep; do dc logs --no-color "$s" > "$dir/$s.log" 2>&1; done
  for s in $(dc ps -a --format '{{.Service}}' | grep '^lez-'); do
    dc logs --no-color "$s" > "$dir/$s.log" 2>&1
  done
  # What each running demo shows, through its inspector (never published).
  for s in $($PY -c "
import json
print(' '.join(s for g in json.load(open('$out/plan.json')) if g['kind']=='delivery-demo' for s in g['services']))"); do
    dc exec -T "$s" python3 /opt/sim/inspector.py screenshot /tmp/screen.png >/dev/null 2>&1 &&
      dc cp "$s:/tmp/screen.png" "$dir/$s.png" >/dev/null 2>&1
  done
  cmd_report "$dir" > "$dir/report.txt" 2>&1 || true
  # Message delivery, when the manifest had a sender (SEND_COUNT in a group's env).
  ls "$dir"/*.send.jsonl >/dev/null 2>&1 &&
    { $PY "$here/lib/msg_report.py" "$dir" > "$dir/delivery.txt" 2>&1 || true; }
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
  run)     shift; cmd_run "$@" ;;
  suite)   shift; cmd_suite "$@" ;;
  start)   shift; cmd_manual start "$@" ;;
  stop)    shift; cmd_manual stop "$@" ;;
  *) usage; exit 1 ;;
esac
