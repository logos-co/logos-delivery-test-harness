# AGENTS.md — operating the logos-delivery test harness

You are driving a docker fleet of logos-delivery nodes: one or more
**bootstraps** (fixed nodes the rest join the DHT through) and **members**, plus
Prometheus and Grafana. Every node is one of three **kinds**: `logosdeliverynode`
(the delivery binary, kademlia in-process), `delivery-module` (a logoscore
daemon hosting `delivery_module`, with discovery hosted on `libp2p_module`), or
`delivery-demo` (the demo UI with its own delivery node; members only). A
request usually reads like *"run a 30 min test with 2 bootstraps and 15 members,
add one after 2 min"*. Your job: turn it into a manifest, run it,
and report what happened with evidence.

The README has the reference material (manifest fields, overrides, commands).
This file is the procedure and the things that are easy to get wrong.

## Ground rules

- **One stack per host.** Services have fixed addresses (`10.0.0.10` first bootstrap,
  `.11` Prometheus, `.12` Grafana) and one compose project. Before starting,
  `docker ps --filter name=logos-delivery-test-harness` must be empty; if it is
  not, ask before tearing someone else's run down.
- **Nix runs only inside the builder container.** Never install or call nix on
  the host for the harness. The `logos-harness-nix` volume is the build cache —
  do not remove it unless asked for a cold build.
- **Scratch manifests go in `out/manifests/`** (gitignored). Only add to
  `examples/` when asked for a reusable topology.
- **Do not edit `out/`** by hand except `out/manifests/`; it is regenerated.
- **Do not edit `harness.sh` while a `build`, `up` or `run` is executing it.**
  bash reads a script as it goes: the running command finishes, then fails on
  whatever the edit left at its old read position.
- **Do not push, open PRs or change the default pins** unless the operator asks.
- `docker/` (entrypoints, `profile.sh`, `inspector.py`) and `conf/` are baked
  into the images: after changing them, `./harness.sh build` (fast when only the
  image changes).

## From request to manifest

Start from the closest file in `examples/` (or `harness.json`) and copy it to
`out/manifests/<name>.json`.

| The request says | In the manifest |
|---|---|
| "N bootstraps", "bootstrap nodes" (a request may still say "seeds": same thing) | N groups of `"role": "bootstrap", "count": 1`, each with its own `ip` (`10.0.0.10`, `10.0.0.20`, `10.0.0.21`, … — never `.11`/`.12`) and its own 64-hex `nodekey`. Leave `peerId` and `port` out: derived / defaulted. Bootstraps 2..N join the first one's DHT automatically. |
| "native", "logosdeliverynode", "standalone node" | `"kind": "logosdeliverynode"` plus `args` — copy the first bootstrap's `args` from `harness.json` |
| "module node", "delivery-module", "logoscore node" | `"kind": "delivery-module"` (optionally `config`, `env`) |
| "demo", "demo UI", "an instance I can watch", "logos-delivery-demo" | `"role": "member", "kind": "delivery-demo"` (same fields as a module member). Needs a `build` with that manifest. Tell the operator the noVNC URLs `gen` prints (`http://localhost:608N/vnc.html`). |
| (kind not said) | leave `kind` out: bootstraps default to `logosdeliverynode`, members to `delivery-module` |
| "M startup members / joiners" | one member group, `"role": "member", "count": M, "startAfter": 0, "jitter": 8` — "M mixed" means two groups, one per kind, splitting M |
| "after X min add a member" | its own member group, `"count": 1, "startAfter": X*60, "jitter": 0` |
| "K peers leave / close / stop at Y min" | those peers in their own group with `"stopAfter": Y*60` — stops apply to whole groups |
| "attach X by hand", "I'll add it myself", "not on the schedule" | `"manual": true` on that group (no `startAfter`/`stopAfter`); after `up`, `./harness.sh start <group\|node>` attaches it and `stop` detaches it |
| "stop a bootstrap at Y min" | `"stopAfter": Y*60` on that bootstrap's group |
| "members only on bootstrap2" | `"bootstraps": ["bootstrap2"]` on the member group (default: all bootstraps, round-robin) |
| "join logos.dev / logos.test", "members on the fleet" | `"bootstraps": []` on that group. Module members: `"config": "member-logosdev.json.tpl"` / `"member-logostest.json.tpl"`. Native members: `"--preset=logos.dev"` / `"--preset=logos.test"` in `args`, and **no** `clusterId` (the harness then drops `--cluster-id`; `--cluster-id=2` would turn any preset into logos.dev). No bootstrap group needed if every member group is on the fleet |
| "on cluster N" (a non-preset network) | `"clusterId": N` on the group, or `network.clusterId` for all; a member must share its bootstraps' cluster. Shards stay manifest-wide (`network.shardsInNetwork`) |
| "members join through every bootstrap", "a bootstrap list" | `"joinAll": true` on the member group (order = its `bootstraps`) |
| "an unreachable / dead bootstrap first" | `"extraBootstraps": ["/ip4/10.0.0.99/tcp/45000/p2p/<id>"]` on the member group; derive `<id>` from any unused nodekey with `lib/manifest.py`'s `peer_id()`. A module member only gets them through a profile with `@BOOTSTRAPS@` (the fleet profiles have none) |
| "a Z min test" | `./harness.sh run <Z*60>` — not a manifest field |
| "on module / delivery / libp2p commit or branch X" | environment variables, see below |

Group names become Prometheus labels and show up in the report: name them after
what they are (`late-2min`, `leave-10min`), not `g3`.

Check before building anything:

```bash
HARNESS_MANIFEST=out/manifests/<name>.json ./harness.sh gen
```

The printout (`+  120s  late-2min  1 node(s)`, `stop at +600s`) must match the
request line by line. A manifest mistake fails here with a `harness:` message
naming the group.

## Revisions

Default: `logos-delivery-module` `master`, everything else from its flake lock,
except `logos-logoscore-cli`, which the manifest pins (`665ac28b`). Override
per component with `<PREFIX>_REF` (branch, tag or **full** 40-char sha) or
`_REV`:

| Component | Prefix | Note |
|---|---|---|
| logos-delivery-module | `DELIVERY_MODULE` | everything else follows its lock |
| logos-delivery | `DELIVERY` | moves both kinds of node |
| libp2p_module | `LIBP2P_MODULE` | |
| openmetrics-module | `OPENMETRICS_MODULE` | |
| logos-logoscore-cli | `LOGOSCORE_CLI` | |
| logos-delivery-demo | `DELIVERY_DEMO` | only for `delivery-demo` groups; default `main`, always built against the fleet's delivery_module |

For a PR, use its head commit, not its branch name:
`gh pr view <n> --repo <owner/repo> --json headRefOid`. Pass the same variables
to `build` and to `run`. `./harness.sh resolve` prints what each component
resolves to and whether it came from the lock or an override — run it and
check it before a long build.

## Build

```bash
./harness.sh build > out/build.log 2>&1
```

Typically 5–20 minutes with a warm cache (a changed logos-delivery rebuilds the
Nim library, the slow part), much longer cold. Rebuild when the pins change or
`docker/`/`conf/` changed. A failed build shows its nix error at the end of the
log.

## Run

```bash
HARNESS_MANIFEST=out/manifests/<name>.json ./harness.sh run <seconds> out/collect/<name>
```

This blocks for the whole run: start it in the background and wait for it to
exit rather than polling with sleeps. It starts Prometheus and Grafana, launches
the groups on schedule, waits until **T0 + seconds**, collects into the
directory, and tears everything down.

Timing, precisely:

- **T0** is when the offset-0 groups are launched, before members wait for
  their bootstrap to become healthy. `out/run.log` and `<dir>/run.log` record T0 and
  every launch and stop with its actual offset.
- `startAfter` / `stopAfter` count from T0. Anything scheduled at or after the
  end of the run is dropped, and `run` prints a warning up front — if you see
  one, the manifest and the duration disagree.
- `up` alone runs the same schedule, returns when the last event has happened,
  and leaves the stack running (for Grafana at `http://localhost:3000`). Always
  finish with `./harness.sh collect` and `./harness.sh down -v`.

**An experiment the operator drives** ("let me attach it myself", "I want to
watch it"): use `up`, not `run`, and leave the stack running. Report what is up,
the noVNC URLs `gen` printed, and the exact `./harness.sh start` / `stop`
commands; do not collect or tear down until asked.

To change the fleet by hand mid-run -- attach a `manual` group, add or take down
any group or node -- use `./harness.sh start <group|node>` / `stop`: they act on
the running stack, refuse to start a member whose bootstrap is down, and log to
`run.log` with the offset from T0.

For a mid-run look without disturbing anything: `./harness.sh collect <dir>`
(the stack keeps running), `./promq.sh '<promql>'`, or the stage lines:
`docker compose -f docker-compose.yml -f out/compose.groups.yml logs --no-color | grep '\[harness'`.

## Reading the results

`<dir>` after a run:

| File | What it tells you |
|---|---|
| `stages.txt` | per delivery-module and delivery-demo node: `load-module … OK`, `createNode OK`, `start OK (Ns)` — start judged by the node's own `nodeStarted` event, not the call — then `subscribe`, `openmetrics` (a delivery-demo logs only `start` and `subscribe`). `start FAILED: <reason>` means the container exited. `module lost: <module> at <utc>` means a module host crashed mid-run. |
| `report.txt` | discovery summary over the delivery-module and delivery-demo nodes' traces (a logosdeliverynode writes none; judge those from Prometheus and their logs): members ready/advertised, first-lookup latency, empty lookups, records per lookup, coverage of the expected id set, per-member table |
| `m*.trace`, `<module bootstrap>.trace` | every call across the discovery plugin boundary, with timestamps, per traced node |
| `ps.txt` | container states at collection; stopped-by-schedule groups show `exited` |
| `mN.png` | a screenshot of each running delivery-demo, taken through its QML inspector at collection |
| `q_up.json`, `q_peers.json`, `q_relay.json` | Prometheus at collection: `up`; `logos_delivery_connected_peers_per_shard` (delivery-module nodes only); relay connections per node, in + out, for both kinds |
| `<bootstrap>.log`, `m*.log` | kept for every bootstrap, every logosdeliverynode, every delivery-demo, and any delivery-module member with an error, a failed stage or a stopped container (`COLLECT_LOGS=all` keeps every log) |
| `resolved.json` | the exact revision of every component — quote it in the report |

Trace lines, from benign to bad:

- `libp2p start NOTICE … timeout (bring-up continues)` — libp2p's 10 s start
  budget ran out under a busy boot; the start keeps running. Expected under
  load. Followed by:
- `REFUSED … switch not started` — a call arrived before that start finished.
  Fine **if** the same call succeeds a second or so later (delivery retries it).
  If it never does, the member is isolated: say so.
- `TRANSPORT-ERR … object_unavailable` — the libp2p_module host is gone.
  Matches a `module lost` line. Always a finding.

## Reporting a run

Lead with the verdict in one sentence, then:

1. **Pins** — every component with its short sha, from `resolved.json`, and
   which ones were overridden.
2. **Topology and schedule** — groups with role, kind and count, start/stop
   offsets, duration, T0 in UTC.
3. **Results** — a table from `report.txt` and the metrics: start outcomes,
   advertised, members whose lookups found peers, distinct ids seen / expected,
   first-lookup latency, empty lookups, peers per shard (min / median), targets up.
   In a mixed fleet, split the peer and `up` figures by `kind` (the Prometheus
   label), and say that the trace-based figures cover delivery-module and
   delivery-demo nodes only.
4. **Anomalies** — by member name, with the trace or log lines that show it.
5. **Comparison** — against the previous run, if there is one, in the same table.

## Known behaviour — do not report these as regressions

- Lookups return at most **30 records**, so with more than 30 other nodes no
  single lookup holds the whole set; coverage reaches it across lookups.
- A fleet that boots together takes **about 5–6 minutes** to reach full
  coverage: registrars make an advertiser wait according to how many address
  bits it shares with ads they already hold. A member joining an
  already-formed fleet gets the full set on its first lookup; one joining a
  forming fleet converges when the fleet does.
- Members are scattered across `10.0.0.0/8` on purpose, for the same reason.
  Do not "fix" the addressing.
- A few `start NOTICE` timeouts per boot are normal and recover within
  seconds; how many varies run to run with host load.
- Peer-count gauges (`logos_delivery_connected_peers*`, per shard and per
  protocol) first appear about **5 minutes** after a node starts, each node on
  its own timer. A run shorter than that collects empty `q_peers.json` /
  `q_relay.json`; judge connectivity from the traces and logs instead.
- A delivery-demo node has no Prometheus target and no `module lost` watchdog;
  judge it by its stage lines, trace and log. Its first build adds ~3.4 GB to
  the nix volume (the Qt closure) — check free space first
  (`docker run --rm alpine df -h /`).
- Registrars keep an advert until it expires (900 s by default), so a stopped
  node's id can keep coming back from lookups for up to that long after the
  stop. That is expected, not a leak.
- Fleet members (`"bootstraps": []`) depend on the preset's entry nodes. When
  most are down (check with `nc -z <entry> 30303`), a delivery-module member's
  libp2p start can outlast the entrypoint's 60 s `START_TIMEOUT` and log
  `start FAILED: no nodeStarted event`. Give fleet groups
  `"env": {"START_TIMEOUT": "300"}`, and report the fleet's state rather than a
  regression. Fleet members also dial out only: they advertise their docker
  address, so the fleet cannot dial them back.
