# logos-delivery-test-harness

A container fleet for exercising logos-delivery: one or more bootstrap nodes and
any number of members, each either a native `logosdeliverynode` or a
delivery-module node (logoscore + delivery_module + libp2p_module), joining and
leaving on a schedule, with Prometheus and provisioned Grafana dashboards.

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
| `logos-delivery` (the logosdeliverynode binary, and what delivery_module is built against) | the module's lock |
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
| logos-delivery-demo (only for `delivery-demo` groups; default `main`, not from the module's lock) | `DELIVERY_DEMO` |

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

#### `DELIVERY_*` moves the whole fleet, not just the native nodes

logos-delivery is built twice over: once directly, as the `logosdeliverynode`
binary the native nodes run, and once *inside* the module's build, where
`delivery_module` — what delivery-module nodes run — is compiled against the module's
`logos-delivery` input. Two separate nix invocations.

So a chosen delivery is also passed to the module's build as
`--override-input logos-delivery …`, and both kinds of node move together. Without
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

> The shipped logosdeliverynode arguments and member profile use the plugin-discovery flags
> (`--enable-kad-discovery`, `--max-pure-libp2p-peers`, `plugin-kad-discovery`).
> A module ref from before that work landed rejects them; change both together.

### Groups

A group is *n nodes of one role and one kind, sharing one profile, launched at
one offset and optionally stopped at another*:

```json
{ "name": "late-5min", "role": "member", "kind": "delivery-module", "count": 1,
  "startAfter": 300, "jitter": 0, "config": "member.json.tpl" }

{ "name": "leavers", "role": "member", "count": 3,
  "startAfter": 0, "stopAfter": 600, "jitter": 8 }
```

- `role` — `bootstrap` (a fixed, known node the others join the DHT through) or
  `member`.
- `kind` — what runs. Any role can be either:

  | `kind` | Runs | Default for |
  |---|---|---|
  | `logosdeliverynode` | the delivery node binary, kademlia in-process; configured by the group's `args` | `bootstrap` |
  | `delivery-module` | a logoscore daemon hosting delivery_module, discovery hosted on libp2p_module; configured by the group's `config` profile | `member` |
  | `delivery-demo` | the [logos-delivery-demo](https://github.com/logos-co/logos-delivery-demo) UI with its own delivery node, which you watch in a browser; configured by the same `config` profile ([below](#demo-nodes)) | — (members only) |

- `count` — n nodes. Bootstrap groups hold exactly one (below).
- `config` (delivery-module) — the profile in `conf/`, rendered per node
  (`@IP@`, `@BOOTSTRAPS@`, `@LOOKUP@`, `@CLUSTER@`, `@SHARDS@`, `@TCP_PORT@`).
- `args` (logosdeliverynode) — the node's behaviour flags. The harness sets
  identity, network, metrics and bootstrap flags itself (`--cluster-id`,
  `--shard`, `--nat`, `--tcp-port`, `--nodekey`, `--metrics-server*`,
  `--kad-bootstrap-node`) and rejects them in `args`.
- `startAfter` — seconds from T0, where T0 is when the offset-0 groups launch.
- `stopAfter` — seconds from T0 at which the group's containers are stopped
  with `docker compose stop`. A delivery-module node's entrypoint passes the
  SIGTERM to the logoscore daemon, which stops its modules and exits; a
  logosdeliverynode gets it directly. Anything still running 10 s later is
  killed. Must be later than `startAfter`. Stops apply to a whole group, so
  peers meant to leave get a group of their own. A stopped container keeps its
  logs and trace for `collect`.
- `manual` (members) — `true` keeps the group off the schedule: `gen` writes
  its services and `build` builds what they need, but `up` and `run` never
  launch it. Attach it to the running stack with `harness.sh start <group>`
  (or one node, `start m4`) and detach it with `stop`, as often as you like. No
  `startAfter` / `stopAfter` on a manual group.
- `bootstraps` (members) — the bootstrap groups this group joins through, handed
  out round-robin across the fleet. Default: every bootstrap group. A
  delivery-module member takes one bootstrap peer, because the plugin passes
  libp2p only the first.
- `jitter` — random 0..n second spread within a delivery-module group. N daemons
  loading plugins at the same instant trip logos-core's 10 s plugin-load timeout.
- `env` — extra environment for the group's delivery-module containers. The rest
  is derived (`BOOTSTRAP_ADDR`, `CLUSTER_ID`, `NUM_SHARDS`, ports), but these are
  only reachable here: `LOOKUP_INTERVAL` (seconds, into the rendered profile),
  `MESH_CONTENT_TOPIC` (what the node subscribes to, default
  `/sim/1/mesh/proto`), `P2P_MAX_CONNS` (the plugin host's connection limits,
  default 100), `LD_DISCO_TRACE` (where the plugin writes its trace).

A **bootstrap** group holds exactly one node, at a fixed address with a fixed
identity, so the others know where to join before it starts:

```json
{ "name": "bootstrap-native", "role": "bootstrap", "kind": "logosdeliverynode", "count": 1,
  "ip": "10.0.0.10",
  "nodekey": "9999999999999999999999999999999999999999999999999999999999999999",
  "args": ["--entry-layer=kernel", "--relay=true", "--enable-kad-discovery=true", "..."] }

{ "name": "bootstrap-module", "role": "bootstrap", "kind": "delivery-module", "count": 1,
  "ip": "10.0.0.20",
  "nodekey": "8888888888888888888888888888888888888888888888888888888888888888" }
```

- `ip` — a fixed address; `10.0.0.11` and `10.0.0.12` are Prometheus and Grafana.
- `nodekey` — 64 hex digits of a secp256k1 key. A logosdeliverynode takes it as
  its node key and is joined on its own port (`port`, default 44001). A
  delivery-module bootstrap hands it to libp2p_module as `privKey`, and is joined
  on libp2p's port 45000 — its DHT runs there, not in delivery. Either way the
  `peerId` is derived from the key; one that is given anyway must match.
- Every bootstrap after the first joins the first one's DHT (the harness adds the
  bootstrap flag, or the profile entry), so all bootstraps form one network.

logosdeliverynode nodes write no discovery trace and no stage log, so `report`
and the `module lost` watchdog cover delivery-module nodes only; `collect` keeps
every logosdeliverynode's log instead, and Prometheus scrapes both kinds.

### Demo nodes

A `delivery-demo` group runs [logos-delivery-demo](https://github.com/logos-co/logos-delivery-demo)
— the educational UI for delivery_module — as a fleet member you can watch and
use in a browser:

```json
{ "name": "demo", "role": "member", "kind": "delivery-demo", "count": 2,
  "startAfter": 0, "stopAfter": 900, "env": { "LOOKUP_INTERVAL": "15" } }
```

It takes the same fields as a delivery-module member (`count`, `startAfter`,
`stopAfter`, `jitter`, `config`, `env`, `bootstraps`) and joins the same way:
the profile is rendered as for a module member, and the node is created with it.
Each instance's screen is published through noVNC on `6080 + n` —
`http://localhost:6080/vnc.html` for the first. `gen` prints the URLs and `run`
logs them at T0.

How it works: the demo never starts a node by itself, so the harness makes the
call its "Advanced config" row makes — `callCreateNodeWithConfig(<profile>)` —
through the QML inspector its host (logos-standalone-app) embeds, and then
subscribes it to `MESH_CONTENT_TOPIC`. The call shows up in the demo's event log
as if the button had been pressed; from there the UI is yours. The stage log
reports `start OK (Ns)` / `start FAILED: …` and `subscribe … OK`, as for module
members.

- **Same delivery as the fleet.** The demo is built against the fleet's
  delivery_module *and* that module's logos-delivery
  (`--override-input delivery_module`, `--override-input
  delivery_module/logos-delivery`); the demo's own pins are not used. Choose the
  demo's revision with `DELIVERY_DEMO_REF` / `_REV` (default `main`); the commit
  it resolved to is in `out/demo-paths.json`.
- **Run from the nix volume.** The demo's closure is ~3.4 GB, so it is not copied
  into an image: demo containers mount the build volume read-only at `/nix`, and
  the `logos-sim-demo:local` image only adds the display (~0.5 GB over the member
  image). What the demo needs is GC-rooted in the volume. It is built only for a
  manifest that has a `delivery-demo` group.
- **Observability.** Traces go to `report` like a module member's; `collect`
  keeps each demo's log (the app's output, `[app]`-prefixed) and a screenshot
  (`mN.png`). No Prometheus target (no CLI into the demo's core to start
  openmetrics with) and no `module lost` watchdog.

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
| `harness.sh collect [dir]` | snapshot a running stack: stage log, container states, traces, plan and pins, `up`, peers-per-shard and relay-connection metrics, report, and the logs of every bootstrap and logosdeliverynode and of any delivery-module member with an error, a failed stage or a stopped container (`COLLECT_LOGS=all` keeps every log) |
| `harness.sh start <group\|node>…` | start groups or nodes in the running stack — a `manual` group, or any other mid-run. Refuses when the bootstrap they join through is not running; logs to `run.log` and prints a demo's noVNC URL |
| `harness.sh stop <group\|node>…` | stop them again (graceful, as for `stopAfter`); `start` brings them back |
| `harness.sh down [-v]` | tear down |
| `harness.sh report [dir] [n]` | discovery report over a trace dir (default `out/traces`); `n` nodes expected in the DHT, default from the plan |

`harness.sh` with no command prints all of this, including the revision
prefixes, which it reads from `lib/manifest.py` so the help cannot drift.

### Everything overridable

Revisions, covered [above](#choosing-revisions-from-the-environment):

| Variable | Overrides |
|---|---|
| `DELIVERY_MODULE_REF` / `_REV` / `_FLAKE` | logos-delivery-module — the pin everything else follows |
| `DELIVERY_REF` / `_REV` / `_FLAKE` | logos-delivery — **both kinds of node** |
| `LIBP2P_MODULE_REF` / `_REV` / `_FLAKE` | libp2p_module |
| `OPENMETRICS_MODULE_REF` / `_REV` / `_FLAKE` | openmetrics-module |
| `LOGOSCORE_CLI_REF` / `_REV` / `_FLAKE` | logos-logoscore-cli |
| `DELIVERY_DEMO_REF` / `_REV` / `_FLAKE` | logos-delivery-demo (default `main`) |

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

**"Run a 30 min test with 2 bootstraps and 15 startup joiners, after 2 min add
another member, after 12 min add another."** — exactly
[`examples/2-bootstraps-15-members-late-2-12min.json`](examples/2-bootstraps-15-members-late-2-12min.json):

```bash
HARNESS_MANIFEST=examples/2-bootstraps-15-members-late-2-12min.json \
  ./harness.sh run 1800 out/collect/2bootstraps-30min
```

**"Two bootstraps, one native and one module, 30 members of both kinds, a late
joiner of each kind, 25 minutes."** — exactly
[`examples/mixed-2-bootstraps-30-members.json`](examples/mixed-2-bootstraps-30-members.json):

```bash
HARNESS_MANIFEST=examples/mixed-2-bootstraps-30-members.json \
  ./harness.sh run 1500 out/collect/mixed-25min
```

**"Same, but 3 of the startup members leave after 10 minutes."** — split them into
their own group with a stop time:

```json
{ "name": "module", "role": "member", "kind": "delivery-module", "count": 12, "startAfter": 0, "jitter": 8 },
{ "name": "leave-10min", "role": "member", "kind": "delivery-module", "count": 3, "startAfter": 0, "stopAfter": 600, "jitter": 8 }
```

**"Stop the module bootstrap at 5 minutes and see whether its members stay
discoverable."** — `"stopAfter": 300` on `bootstrap-module`. Its members keep running.

**"Put the late joiners on the native bootstrap only."** —
`"bootstraps": ["bootstrap-native"]` on those groups.

**"Only native nodes."** — every group `"kind": "logosdeliverynode"`, members
with the same `args` as the bootstrap. `report` then has no traces to read, so
judge the run from Prometheus and the node logs.

**"Rerun the 37-node test on delivery PR #1234 for 25 minutes."** — take the PR's
head commit (`gh pr view 1234 --repo logos-messaging/logos-delivery --json headRefOid`)
and override delivery, which moves both kinds of node together:

```bash
DELIVERY_REF=<full sha> HARNESS_MANIFEST=examples/37-node-late-2-12min.json \
  ./harness.sh build
DELIVERY_REF=<full sha> HARNESS_MANIFEST=examples/37-node-late-2-12min.json \
  ./harness.sh run 1500 out/collect/pr1234
```

**"A 10 minute smoke on logos-delivery-module commit X."** — the default manifest
(1 bootstrap, 3 members):

```bash
DELIVERY_MODULE_REF=<full sha> ./harness.sh build
DELIVERY_MODULE_REF=<full sha> ./harness.sh run 600
```

**"Add two demo UIs I can watch, one joining after 2 minutes and leaving at 8."** —
[`examples/demo-2-instances.json`](examples/demo-2-instances.json); open the
printed `http://localhost:608N/vnc.html` URLs while it runs:

```bash
HARNESS_MANIFEST=examples/demo-2-instances.json ./harness.sh build
HARNESS_MANIFEST=examples/demo-2-instances.json ./harness.sh run 600 out/collect/demo
```

**"Bring up the fleet, and let me attach a demo by hand when I want."** —
[`examples/demo-manual.json`](examples/demo-manual.json) has a `manual` demo group:

```bash
HARNESS_MANIFEST=examples/demo-manual.json ./harness.sh build
HARNESS_MANIFEST=examples/demo-manual.json ./harness.sh up
./harness.sh start demo      # attach; the noVNC URL is printed
./harness.sh stop demo       # detach; start it again whenever
```

**"Did anyone lose libp2p?"** — after a run, `grep 'module lost' <dir>/stages.txt`;
`collect` kept that member's log.
