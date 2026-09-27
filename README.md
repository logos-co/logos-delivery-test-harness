# logos-delivery-test-harness

A container fleet for exercising logos-delivery: one or more seeds, any number
of plugin-hosted member nodes joining and leaving on a schedule, Prometheus and
provisioned Grafana dashboards.

Driving it from an agent (Claude Code, Codex, ...)? [AGENTS.md](AGENTS.md) is the
operating manual, and [Prompt examples](#prompt-examples) below shows requests it
turns into runs.

Everything is built by nix from named git revisions, so a manifest plus the
generated `out/resolved.json` reproduces an image exactly.

## Requirements

Docker, bash, python3. **Nix is not needed on the host** — it runs inside the
builder container, whose store persists in the `logos-harness-nix` volume.

## Quickstart

```bash
./harness.sh build          # resolve, nix-build every component, build logos-sim:local
./harness.sh up             # launch the groups on their schedule, leave the stack up
open http://localhost:3000    # Grafana (anonymous admin)
./harness.sh down -v

./harness.sh run 900        # or: up, 15 min from T0, collect into out/collect/<stamp>, down
```

`HARNESS_MANIFEST=examples/37-node-late-joiners.json ./harness.sh run 1500` runs
the 37-node late-joiner topology instead of the small default.

## The manifest

`harness.json` pins exactly **one** thing — the logos-delivery-module ref — and
declares the node groups. Every other component is derived from that module's own
flake lock, so the harness always builds a coherent set:

| Component | Where its revision comes from |
|---|---|
| `logos-delivery-module` | `module.ref` in the manifest — `master` by default (branch, tag or `rev`) |
| `logos-delivery` (the seed binary, and what delivery_module is built against) | the module's lock |
| `libp2p_module` | the module's lock |
| `openmetrics-module` | the module's lock |
| `logos-logoscore-cli` | `overrides` — see [Overrides](#overrides) |

### Choosing revisions from the environment

Every component takes `<PREFIX>_REF`, `<PREFIX>_REV` and `<PREFIX>_FLAKE`:

| Component | Prefix |
|---|---|
| logos-delivery-module | `DELIVERY_MODULE` |
| logos-delivery | `DELIVERY` |
| libp2p_module | `LIBP2P_MODULE` |
| openmetrics-module | `OPENMETRICS_MODULE` |
| logos-logoscore-cli | `LOGOSCORE_CLI` |

```bash
DELIVERY_MODULE_REF=my-branch              ./harness.sh build   # branch
DELIVERY_MODULE_REF=v1.4.0                 ./harness.sh build   # tag
DELIVERY_MODULE_REF=ae967a12…78b9b321d     ./harness.sh build   # commit (full sha)
DELIVERY_MODULE_REV=ae967a12…78b9b321d     ./harness.sh build   # commit, explicitly
DELIVERY_MODULE_FLAKE=git+https://…/my-fork ./harness.sh build  # another repo

# Pin one dependency away from what the module chose, leaving the rest derived
DELIVERY_REF=my-fix-branch                 ./harness.sh build
LIBP2P_MODULE_REV=ec7b8f58…239661805d      ./harness.sh build
```

`_REF` takes a branch, a tag, **or** a full 40-character sha — a sha is
recognised and passed as a `rev`, because nix does not accept one as a `ref`.
Short shas are not: give the full object name, or use a tag. `_REV` says commit
explicitly. Naming one clears the other, so a `_REF` on the command line beats a
`rev` in the manifest.

Setting a component's variables is the same thing as writing an override, so
`harness.sh resolve` reports it as `override`. Anything left alone stays
`module-lock` — **the module keeps driving its dependencies unless you say
otherwise**, which is the point of the design.

#### `DELIVERY_*` moves the whole fleet, not just the seed

logos-delivery is built twice over: once directly, as the `logosdeliverynode`
binary the seed runs, and once *inside* the module's build, where
`delivery_module` — what the members run — is compiled against the module's
`logos-delivery` input. Two separate nix invocations.

So a chosen delivery is also passed to the module's build as
`--override-input logos-delivery …`, and seed and members move together. Without
that the fleet would quietly be built from two different deliveries, which is
not something either `resolved.json` or a running node would show you.

This is specific to delivery. The other components are runtime artefacts — a
chosen `libp2p_module` is the one dropped into `/opt/modules` and loaded, so
there is nothing to propagate.

> A `_REF` naming a branch is not a pin: `resolved.json` records the branch, and
> two builds a day apart can differ. `_REV` (or a sha in `_REF`) is what makes a
> build reproducible.

### Overrides

`master` pins `logos-delivery`, `libp2p_module` and `openmetrics-module` in its
flake lock, so only one override ships — `logos-logoscore-cli`. That one is not a
gap to fill but a choice: the CLI is not an input of the module itself, its lock
only carries copies pulled in by other inputs (via `libp2p_module` and
`logos-test-modules`, at different revisions), and its docker-compose runs a third.
The harness stays on `665ac28b`, the version every recorded run used.

A ref that does not pin a component needs it supplied here, with a full `flake`
reference:

```json
"overrides": {
  "libp2p_module": { "flake": "git+https://github.com/logos-co/logos-libp2p-module",
                     "rev": "ec7b8f58..." }
}
```

An override carrying a full `flake` stands alone and needs no lock entry; one
carrying only `rev` swaps the revision of a component the module does pin.
`harness.sh resolve` reports each component's source as `module-lock`,
`override` or `manifest`, so what is derived and what is imposed is visible
before anything is built.

> The shipped seed arguments and member profile use the plugin-discovery flags
> (`--enable-kad-discovery`, `--max-pure-libp2p-peers`, `plugin-kad-discovery`).
> A module ref from before that work landed rejects them; change both together.

### Groups

A group is *n nodes sharing one profile, launched at one offset and optionally
stopped at another*:

```json
{ "name": "late-5min", "kind": "member", "count": 1,
  "startAfter": 300, "jitter": 0, "config": "member.json.tpl" }

{ "name": "leavers", "kind": "member", "count": 3,
  "startAfter": 0, "stopAfter": 600, "jitter": 8 }
```

- `kind` — `seed` (native `logosdeliverynode`, in-process kademlia) or `member`
  (logos-core daemon hosting delivery_module + libp2p_module).
- `count` + `config` — n nodes with profile x, m nodes with profile y. Profiles
  live in `conf/` and are rendered per node (`@IP@`, `@SEED@`, `@LOOKUP@`,
  `@CLUSTER@`, `@SHARDS@`, `@TCP_PORT@`).
- `startAfter` — seconds from T0, where T0 is when the offset-0 groups launch.
- `stopAfter` — seconds from T0 at which the group's containers are stopped
  with `docker compose stop`. A member's entrypoint passes the SIGTERM to the
  logoscore daemon, which stops its modules and exits; a seed gets it directly.
  Anything still running 10 s later is killed. Must be later than `startAfter`. Stops apply to a whole group, so peers meant to leave get a group
  of their own. A stopped container keeps its logs and trace for `collect`.
- `seeds` (members only) — the seed groups this group bootstraps from, handed
  out round-robin across the fleet. Default: every seed group. Each member gets
  one bootstrap peer, because the plugin passes libp2p only the first.
- `jitter` — random 0..n second spread within the group. N daemons loading
  plugins at the same instant trip logos-core's 10 s plugin-load timeout.
- `env` — extra environment for the group's containers. The rest of a member's
  settings are derived (`SEED_ADDR`, `CLUSTER_ID`, `NUM_SHARDS`, ports), but
  these are only reachable here: `LOOKUP_INTERVAL` (seconds, into the rendered
  profile), `MESH_CONTENT_TOPIC` (what the member subscribes to, default
  `/sim/1/mesh/proto`), `P2P_MAX_CONNS` (the plugin host's connection limits,
  default 100), `LD_DISCO_TRACE` (where the plugin writes its trace).

A **seed** group holds exactly one seed, the native `logosdeliverynode`:

```json
{ "name": "seed2", "kind": "seed", "count": 1, "ip": "10.0.0.20", "port": 44001,
  "nodekey": "8888888888888888888888888888888888888888888888888888888888888888",
  "args": ["--entry-layer=kernel", "--relay=true", "--enable-kad-discovery=true", "..."] }
```

- `ip` — a fixed address; `10.0.0.11` and `10.0.0.12` are Prometheus and Grafana.
- `nodekey` — 64 hex digits of a secp256k1 key. `peerId` is derived from it; one
  that is given anyway must match.
- `args` — the seed's command line after the identity flags. Copy the first seed's.
- Every seed after the first gets `--kad-bootstrap-node=<first seed>` added, so all
  seeds share one DHT rather than each forming its own network.

The group name becomes a Prometheus label, so dashboards can compare late joiners
against steady-state nodes without hand-written queries.

Members are scattered across the subnet on purpose: the nim-libp2p registrar
scores an advertiser by how many address-prefix bits it shares with the ads it
already holds, and every shared bit costs `advertExpiry/32` of waiting.
Sequential addresses in one /24 share ~27 bits and cost 13–15 minutes per
registration.

## Commands

| Command | Does |
|---|---|
| `harness.sh resolve` | read the module's flake lock, write `out/resolved.json` |
| `harness.sh build [--no-image]` | resolve, nix-build every component, build the image |
| `harness.sh gen` | write `out/compose.groups.yml`, `out/prom-targets.json`, `out/plan.json` |
| `harness.sh up` | gen, start monitoring, launch each group at its `startAfter` and stop it at its `stopAfter`, log to `out/run.log`; returns when the schedule is done and leaves the stack up |
| `harness.sh run <sec> [dir]` | `up`, wait until T0 + `sec`, `collect` into `dir` (default `out/collect/<utc-stamp>`), `down -v`. Launches or stops scheduled at or after the end are dropped, with a warning |
| `harness.sh collect [dir]` | snapshot a running stack: stage log, container states, traces, plan and pins, `up` and peers-per-shard metrics, report, and the logs of the seeds and of any member with an error, a failed stage or a stopped container (`COLLECT_LOGS=all` keeps every log) |
| `harness.sh down [-v]` | tear down |
| `harness.sh report [dir] [n]` | discovery report over a trace dir (default `out/traces`); `n` nodes expected in the DHT, default from the plan |

`harness.sh` with no command prints all of this, including the revision
prefixes, which it reads from `lib/manifest.py` so the help cannot drift.

### Everything overridable

Revisions, covered [above](#choosing-revisions-from-the-environment):

| Variable | Overrides |
|---|---|
| `DELIVERY_MODULE_REF` / `_REV` / `_FLAKE` | logos-delivery-module — the pin everything else follows |
| `DELIVERY_REF` / `_REV` / `_FLAKE` | logos-delivery — **seed and members both** |
| `LIBP2P_MODULE_REF` / `_REV` / `_FLAKE` | libp2p_module |
| `OPENMETRICS_MODULE_REF` / `_REV` / `_FLAKE` | openmetrics-module |
| `LOGOSCORE_CLI_REF` / `_REV` / `_FLAKE` | logos-logoscore-cli |

How the harness itself runs:

| Variable | Default | Overrides |
|---|---|---|
| `HARNESS_MANIFEST` | `./harness.json` | which manifest is read — how the examples are run |
| `BUILDER_IMAGE` | `nixos/nix:2.24.9` | the container every nix command runs in |
| `NIX_VOLUME` | `logos-harness-nix` | the docker volume holding the builder's nix store |
| `IMAGE` | `logos-sim:local` | the runtime image `build` produces and compose runs |
| `PYTHON` | `python3` | the interpreter for `lib/` |
| `SUBNET` | `10.0.0.0/8` | the compose network, read by `docker-compose.yml` |
| `PROMETHEUS_PORT` | `9090` | published Prometheus port |
| `GRAFANA_PORT` | `3000` | published Grafana port |

`NIX_VOLUME` is worth knowing about: it is where every build is cached, so two
checkouts sharing it rebuild almost nothing, and `docker volume rm` on it is
how you force a cold build.

## Layout

```
harness.sh           CLI
harness.json         default manifest
lib/                 manifest parsing, lock resolution, generators
docker/              Dockerfile, member entrypoint
conf/                node profiles
monitoring/          Prometheus config, Grafana provisioning + dashboards
examples/            ready-made topologies
out/                 gitignored: build artefacts, generated compose, traces, run log
```

## Provenance

`out/resolved.json` records the exact flake reference used for every component,
and `out/run.log` records T0 and each group's actual launch and stop time. `run`
and `collect` copy both into the collect directory, next to the measurements.

## Prompt examples

Requests an agent working in this repo can turn into a run (see
[AGENTS.md](AGENTS.md) for how). Each is a manifest plus one command.

**"Run a 30 min test with 2 seeds and 15 startup joiners, after 2 min add another
member, after 12 min add another."** — exactly
[`examples/2-seeds-15-members-late-2-12min.json`](examples/2-seeds-15-members-late-2-12min.json):

```bash
HARNESS_MANIFEST=examples/2-seeds-15-members-late-2-12min.json \
  ./harness.sh run 1800 out/collect/2seeds-30min
```

**"Same, but 3 of the startup members leave after 10 minutes."** — split them into
their own group with a stop time:

```json
{ "name": "members", "kind": "member", "count": 12, "startAfter": 0, "jitter": 8 },
{ "name": "leave-10min", "kind": "member", "count": 3, "startAfter": 0, "stopAfter": 600, "jitter": 8 }
```

**"Stop the second seed at 5 minutes and see whether its members stay discoverable."**
— `"stopAfter": 300` on `seed2`. Its members keep running.

**"Put the late joiners on seed2 only."** — `"seeds": ["seed2"]` on those groups.

**"Rerun the 37-node test on delivery PR #1234 for 25 minutes."** — take the PR's
head commit (`gh pr view 1234 --repo logos-messaging/logos-delivery --json headRefOid`)
and override delivery, which moves the seed and the members together:

```bash
DELIVERY_REF=<full sha> HARNESS_MANIFEST=examples/37-node-late-2-12min.json \
  ./harness.sh build
DELIVERY_REF=<full sha> HARNESS_MANIFEST=examples/37-node-late-2-12min.json \
  ./harness.sh run 1500 out/collect/pr1234
```

**"A 10 minute smoke on logos-delivery-module commit X."** — the default manifest
(1 seed, 3 members):

```bash
DELIVERY_MODULE_REF=<full sha> ./harness.sh build
DELIVERY_MODULE_REF=<full sha> ./harness.sh run 600
```

**"Did anyone lose libp2p?"** — after a run, `grep 'module lost' <dir>/stages.txt`;
`collect` kept that member's log.
