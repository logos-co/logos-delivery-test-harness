# AGENTS.md — operating the logos-delivery test harness

You are driving a docker fleet of logos-delivery nodes: one or more **seeds**
(native `logosdeliverynode`) and **members** (a logoscore daemon hosting
`delivery_module`, with discovery hosted on `libp2p_module`), plus Prometheus
and Grafana. A request usually reads like *"run a 30 min test with 2 seeds and
15 members, add one after 2 min"*. Your job: turn it into a manifest, run it,
and report what happened with evidence.

The README has the reference material (manifest fields, overrides, commands).
This file is the procedure and the things that are easy to get wrong.

## Ground rules

- **One stack per host.** Services have fixed addresses (`10.0.0.10` seed,
  `.11` Prometheus, `.12` Grafana) and one compose project. Before starting,
  `docker ps --filter name=logos-delivery-test-harness` must be empty; if it is
  not, ask before tearing someone else's run down.
- **Nix runs only inside the builder container.** Never install or call nix on
  the host for the harness. The `logos-harness-nix` volume is the build cache —
  do not remove it unless asked for a cold build.
- **Scratch manifests go in `out/manifests/`** (gitignored). Only add to
  `examples/` when asked for a reusable topology.
- **Do not edit `out/`** by hand except `out/manifests/`; it is regenerated.
- **Do not push, open PRs or change the default pins** unless the operator asks.
- `docker/entrypoint-member.sh` and `conf/` are baked into the image: after
  changing them, `./harness.sh build` (fast when only the image changes).

## From request to manifest

Start from the closest file in `examples/` (or `harness.json`) and copy it to
`out/manifests/<name>.json`.

| The request says | In the manifest |
|---|---|
| "N seeds" | N groups of `"kind": "seed", "count": 1`, each with its own `ip` (`10.0.0.10`, `10.0.0.20`, `10.0.0.21`, … — never `.11`/`.12`), its own 64-hex `nodekey`, `port` 44001, and the first seed's `args`. Leave `peerId` out: it is derived from the nodekey. Seeds 2..N join seed 1's DHT automatically. |
| "M startup members / joiners" | one member group, `"count": M, "startAfter": 0, "jitter": 8` |
| "after X min add a member" | its own member group, `"count": 1, "startAfter": X*60, "jitter": 0` |
| "K peers leave / close / stop at Y min" | those peers in their own group with `"stopAfter": Y*60` — stops apply to whole groups |
| "stop a seed at Y min" | `"stopAfter": Y*60` on that seed's group |
| "members only on seed2" | `"seeds": ["seed2"]` on the member group (default: all seeds, round-robin) |
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
| logos-delivery | `DELIVERY` | moves the seed **and** the members |
| libp2p_module | `LIBP2P_MODULE` | |
| openmetrics-module | `OPENMETRICS_MODULE` | |
| logos-logoscore-cli | `LOGOSCORE_CLI` | |

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
  their seed to become healthy. `out/run.log` and `<dir>/run.log` record T0 and
  every launch and stop with its actual offset.
- `startAfter` / `stopAfter` count from T0. Anything scheduled at or after the
  end of the run is dropped, and `run` prints a warning up front — if you see
  one, the manifest and the duration disagree.
- `up` alone runs the same schedule, returns when the last event has happened,
  and leaves the stack running (for Grafana at `http://localhost:3000`). Always
  finish with `./harness.sh collect` and `./harness.sh down -v`.

For a mid-run look without disturbing anything: `./harness.sh collect <dir>`
(the stack keeps running), `./promq.sh '<promql>'`, or the stage lines:
`docker compose -f docker-compose.yml -f out/compose.groups.yml logs --no-color | grep '\[harness'`.

## Reading the results

`<dir>` after a run:

| File | What it tells you |
|---|---|
| `stages.txt` | per member: `load-module … OK`, `createNode OK`, `start OK (Ns)` — start judged by the node's own `nodeStarted` event, not the call — then `subscribe`, `openmetrics`. `start FAILED: <reason>` means the container exited. `module lost: <module> at <utc>` means a module host crashed mid-run. |
| `report.txt` | discovery summary: members ready/advertised, first-lookup latency, empty lookups, records per lookup, coverage of the expected id set, per-member table |
| `m*.trace` | every call across the discovery plugin boundary, with timestamps |
| `ps.txt` | container states at collection; stopped-by-schedule groups show `exited` |
| `q_up.json`, `q_peers.json` | Prometheus `up` and `logos_delivery_connected_peers_per_shard` at collection |
| `seed*.log`, `m*.log` | kept for every seed and for any member with an error, a failed stage or a stopped container |
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
2. **Topology and schedule** — groups, counts, start/stop offsets, duration,
   T0 in UTC.
3. **Results** — a table from `report.txt` and the metrics: start outcomes,
   advertised, members whose lookups found peers, distinct ids seen / expected,
   first-lookup latency, empty lookups, peers per shard (min / median), targets up.
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
- Registrars keep an advert until it expires (900 s by default), so a stopped
  node's id can keep coming back from lookups for up to that long after the
  stop. That is expected, not a leak.
