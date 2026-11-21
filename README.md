# logos-delivery-test-harness

A container fleet for exercising logos-delivery on one machine. A JSON manifest
describes the nodes; the harness builds them from pinned sources, runs them on a
private docker network on a schedule, sends traffic through them, and collects
what happened.

Driving it from an agent (Claude Code, Codex, ...)? [AGENTS.md](AGENTS.md) is the
operating manual, and [Prompt examples](#prompt-examples) below shows requests it
turns into runs.

Everything is built by nix from named git revisions, so a manifest plus the
generated `out/resolved.json` reproduces an image exactly.

## What it does

| Capability | How | Section |
|---|---|---|
| Fleets of mixed node kinds | native `logosdeliverynode`, `delivery-module` (logoscore + delivery_module + libp2p_module), `delivery-demo` (the demo UI), `sd-agent` (libp2p_module alone) | [Groups](#groups) |
| Schedules and churn | groups start and stop at offsets from T0; `start` / `stop` by hand mid-run | [Groups](#groups), [Running](#running) |
| Any revision of any component | one module pin, the rest from its flake lock; per-component overrides in the manifest or the environment | [Building](#building) |
| Several builds side by side | a manifest names its image, so a PR build does not replace the default | [Building](#building) |
| Message traffic with a verdict | sender groups, per-message outcome matched across nodes, latency percentiles, expected outcome per group | [Traffic](#traffic-and-what-is-expected-of-it) |
| Mix | fixed-key mix nodes (native or module), static or discovery-fed pools, exit and retry data per message | [Mix](#mix) |
| RLN | a local LEZ chain with an RLN registry, Relay RLN and per-hop Mix RLN | [RLN](#rln-on-a-local-lez-chain) |
| Service-discovery experiments | `sd-agent` nodes advertising, flapping and looking up services | [sd-agent nodes](#sd-agent-nodes) |
| Public networks | members joining logos.dev or logos.test | [Joining a public network](#joining-a-public-network) |
| Suites | manifests run one after another, each for its own duration, with a summary of verdicts | [Running](#running) |
| Metrics | Prometheus and provisioned Grafana dashboards; CPU and memory per container | [Results](#results) |

master holds what the harness can do. Campaigns run with it (their manifests,
run cases and reports) live on branches of their own, each adding one directory
under `experiments/`:

| Branch | Campaign |
|---|---|
| `experiment/sd-corner-cases` | service-discovery stress and corner cases |
| `experiment/mix-over-delivery` | mix scenarios, long-run mix networks, Mix RLN |

## Requirements

Docker, bash, python3. **Nix is not needed on the host** — it runs inside the
builder container, whose store persists in the `logos-harness-nix` volume.

An RLN build also uses the host's docker socket from a build container (the LEZ
guest programs are built by `cargo risczero` in docker).

Sizing, measured on a 10-core, 12.6 GB docker VM: 46 nodes without RLN used
about 2 cores and 4.2 GiB; 22 RLN nodes with Mix RLN at 0.8 messages per second
used about 4.3 cores; 45 RLN nodes at 1.6 messages per second saturated the
machine and gave no valid result.

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

A manifest from the [examples](#examples) carries its own
build and duration:

```bash
export HARNESS_MANIFEST=examples/mix-static-smoke.json
./harness.sh build          # builds the image this manifest names
./harness.sh run            # for the manifest's "duration"; verdict in <dir>/delivery.txt
```

## Building

`./harness.sh build` does everything the manifest in `HARNESS_MANIFEST` needs,
and each step is skipped or cached when its result exists.

| Step | When | Result |
|---|---|---|
| Resolve the module's flake lock | always | `out/resolved.json`: one flake reference per component |
| `nix build` each component in the builder container | always (cached in the `logos-harness-nix` volume) | `out/logoscore`, `out/modules`, `out/seed-store` |
| RLN modules (`liblogos_rln_module`, `liblogos_lez_rln_module`) | manifest has an `"rln"` section | copied into `out/modules` |
| LEZ sequencer, from source | `"rln"` section, image missing | `logos-sim-lez-sequencer:local` |
| logos-lez-rln tools and guest programs | `"rln"` section, `out/lez-tools` missing | `out/lez-tools`, `logos-sim-lez:local` |
| logos-delivery-demo | manifest has a `delivery-demo` group | rooted in the nix volume, `logos-sim-demo:local` |
| Runtime image | unless `--no-image` | the manifest's `image` (default `logos-sim:local`) |

### Build variants

What is built is decided by three things in the manifest: `module` (the one
pin), `overrides` (components moved away from the module's lock) and `image`
(the tag the result gets).

| Variant | In the manifest | Or from the environment |
|---|---|---|
| The module's own set | nothing | — |
| Another logos-delivery-module | `"module": {"ref": "<branch\|tag>"}` or `"rev"` | `DELIVERY_MODULE_REF` / `_REV` / `_FLAKE` |
| Another logos-delivery (a PR, a branch) | `"overrides": {"logos-delivery": {"rev": "<full sha>"}}` | `DELIVERY_REF` / `_REV` / `_FLAKE` |
| Another libp2p_module, openmetrics-module, logoscore CLI, demo | `"overrides": {"<component>": {...}}` | `LIBP2P_MODULE_*`, `OPENMETRICS_MODULE_*`, `LOGOSCORE_CLI_*`, `DELIVERY_DEMO_*` |
| With RLN | `"rln": {}` | — |
| Its own image | `"image": "logos-sim:pr4647"` | `IMAGE` |

Give every variant its own `image`. The nodes of a manifest run the image it
names, so a manifest pinned to a PR and one on the default set can be built
once each and then run in any order:

```json
{
  "image": "logos-sim:pr4647",
  "overrides": { "logos-delivery": { "rev": "69f3ff21866aaf302caa2a5f080ed08ef715e2d2" } }
}
```

`up`, `run` and `suite` refuse to start when the manifest's image is not built,
and name the build command. The environment wins over the manifest, for the
image and for every revision. An image built with the RLN modules also runs
manifests without RLN; the reverse does not work.

`docker/` and `conf/` are baked into the image: after changing them, build
again (minutes, when the nix results are cached).

Build times: 5–20 minutes with a warm nix volume, much longer cold. The first
RLN build adds over an hour (the sequencer and the LEZ tools compile from
source). The public nix cache (`cache.nix.logos.co`) is used when it answers;
when it does not, nix falls back to building from source after 5 s per lookup.

The LEZ sequencer and tools are built for the host's platform. On Apple silicon
the upstream amd64 sequencer does not run under emulation, which is why the
default is a local build; name another image in `rln.sequencerImage` to use a
prebuilt one.

## Running

```bash
HARNESS_MANIFEST=<manifest> ./harness.sh run [seconds] [collect-dir]
```

`run` brings the stack up, lets it run until T0 + `seconds`, collects into the
directory (default `out/collect/<utc-stamp>`) and tears it down. `seconds` may
be left out when the manifest has a `"duration"`. It blocks for the whole run.

To keep the stack up and look around: `up`, then Grafana at
`http://localhost:3000`, `./promq.sh '<promql>'`, `./harness.sh collect`, and
`./harness.sh down -v` at the end. `start <group|node>` and `stop` change the
running fleet by hand.

One stack runs per host. To run several manifests, use a suite:

```bash
./harness.sh suite --build examples/msg-smoke.json examples/mix-static-smoke.json \
                           examples/rln-smoke.json
```

Each manifest runs for its `duration` and is collected into
`out/collect/suite-<utc-stamp>/<name>/`; `--build` first builds the images that
are missing. The summary (`summary.txt`) has one line per manifest: `PASS` or
`FAIL` from its delivery report, `no traffic to judge` for a manifest without a
sender, or `RUN FAILED`. The exit status is 0 only when nothing failed.

## Examples

One manifest per capability; copy the closest one. "Build" is what the manifest
pins: the mix and RLN examples name logos-delivery master `39b5f4ee`, because
the module's flake lock still points at an older logos-delivery.

| Manifest | Nodes | Build (image) | Duration | Shows |
|---|---|---|---|---|
| [`msg-smoke`](examples/msg-smoke.json) | 5 | module's lock (`logos-sim:local`) | 150 s | plain sends: propagated, stored, received |
| [`sd-smoke`](examples/sd-smoke.json) | 5 | module's lock | 300 s | `sd-agent` nodes: 2 advertisers, 1 looker, a delivery bystander; read with `lib/sd_report.py` |
| [`mix-static-smoke`](examples/mix-static-smoke.json) | 8 | master `39b5f4ee` | 240 s | `Required` sends through 4 fixed-key mix nodes |
| [`rln-smoke`](examples/rln-smoke.json) | 5 | master `39b5f4ee` + RLN | 420 s | Relay RLN: a member's sends arrive, a non-member's all fail (`expect: error`) |
| [`mix-rln-smoke`](examples/mix-rln-smoke.json) | 8 | master `39b5f4ee` + RLN | 480 s | mix over the RLN network, 4 module mix nodes; verdict FAIL on this build (see [Mix](#mix)) |
| [`37-node-late-joiners`](examples/37-node-late-joiners.json), [`37-node-late-2-12min`](examples/37-node-late-2-12min.json), [`2-bootstraps-15-members-late-2-12min`](examples/2-bootstraps-15-members-late-2-12min.json), [`mixed-2-bootstraps-30-members`](examples/mixed-2-bootstraps-30-members.json) | 17–37 | module's lock | 25–30 min | discovery with late joiners, two bootstraps, mixed kinds; no traffic, read `report.txt` |
| [`demo-2-instances`](examples/demo-2-instances.json), [`demo-manual`](examples/demo-manual.json) | 5–6 | module's lock | 10 min / by hand | demo UIs as members |

`lib/gen_mixnet.py` writes a mix network of any size (`--mix`, `--senders`,
`--receivers`, `--mix-kind native|module`, `--rln`, `--stop N --stop-at S`,
`--delivery-rev`, `--image`, `--duration`). `--base <manifest>` takes the
build, chain, network, bootstrap and allocator from a manifest and resizes its
mix, receiver and sender groups; `--allocator-funding N` sets the allocator's
`ALLOCATOR_FUNDING`; `--spread` sets `network.spread` and spreads the mix nodes
the same way.

A logos-delivery that restricts mix to public addresses needs
`--mix-allow-all-addresses=true` on native mix nodes and `MIX_ALLOW_ALL=1` on
module ones; a build without that option rejects it.

## The manifest

`harness.json` pins exactly **one** thing — the logos-delivery-module ref — and
declares the node groups. Every other component is derived from that module's own
flake lock, so the harness always builds a coherent set.

| Top-level key | Default | Meaning |
|---|---|---|
| `module` | required (`flake`; `ref` defaults to `master`) | the logos-delivery-module to build: `ref` (branch or tag) or `rev` |
| `overrides` | none | components taken from somewhere other than the module's lock ([Overrides](#overrides)) |
| `image` | `logos-sim:local` | the runtime image `build` tags and the nodes run ([Build variants](#build-variants)) |
| `duration` | none | seconds `run` and `suite` let the stack run, from T0 |
| `rln` | absent | a local LEZ chain with an RLN registry ([RLN](#rln-on-a-local-lez-chain)) |
| `network` | see [Network](#network) | subnet, cluster, shard count |
| `groups` | required | the nodes ([Groups](#groups)) |

Keys starting with `_` (`_comment`) are ignored.

Where each component's revision comes from:

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
carrying only `rev` (or `ref`) swaps the revision of a component the module does
pin. That is how a manifest runs a logos-delivery PR — its head commit, with an
image of its own:

```json
"image": "logos-sim:pr4647",
"overrides": {
  "logos-delivery": { "rev": "69f3ff21866aaf302caa2a5f080ed08ef715e2d2" }
}
```

As with `DELIVERY_*`, this moves both the native nodes and the delivery_module
the module nodes run.
`harness.sh resolve` reports each component's source as `module-lock`,
`override` or `manifest`, so what is derived and what is imposed is visible
before anything is built.

> The shipped logosdeliverynode arguments and member profile use the plugin-discovery flags
> (`--enable-kad-discovery`, `--max-pure-libp2p-peers`, `plugin-kad-discovery`).
> A module ref from before that work landed rejects them; change both together.

### Network

`network` holds what every node shares:

- `subnet` (default `10.0.0.0/8`) — members get addresses scattered across it
  ([why](#members-are-scattered)). It must match the compose network, set by
  `SUBNET` (same default).
- `clusterId` (default `42`) — the cluster of every group that does not set
  its own.
- `shardsInNetwork` (default `1`) — the shard count, rendered into the module
  profiles as `@SHARDS@`. Not per group: every node of a network must shard the
  same way. logosdeliverynode nodes always get `--shard=0`.
- `spread` (default `false`) — instead of the hash scatter, give each member
  the address sharing the shortest prefix with the bootstraps and the members
  placed before it (`manifest.spread_addresses`). Inside one /8 the registrar's
  IP score is that shared prefix, so this gives the shortest first-advert
  waits the subnet allows (13 shared bits at most for 34 nodes).

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
- `kind` — what runs. A bootstrap is `logosdeliverynode`, `delivery-module` or
  `sd-agent`; a member can be any of the four:

  | `kind` | Runs | Default for |
  |---|---|---|
  | `logosdeliverynode` | the delivery node binary, kademlia in-process; configured by the group's `args` | `bootstrap` |
  | `delivery-module` | a logoscore daemon hosting delivery_module, discovery hosted on libp2p_module; configured by the group's `config` profile | `member` |
  | `delivery-demo` | the [logos-delivery-demo](https://github.com/logos-co/logos-delivery-demo) UI with its own delivery node, which you watch in a browser; configured by the same `config` profile ([below](#demo-nodes)) | — (members only) |
| `sd-agent` | a logoscore daemon hosting libp2p_module alone, driven through its service-discovery API by the group's `SD_*` env ([below](#sd-agent-nodes)) | — |

- `count` — n nodes. Bootstrap groups hold exactly one (below).
- `config` (delivery-module, delivery-demo) — the profile in `conf/`, rendered
  per node (`@IP@`, `@BOOTSTRAPS@`, `@LOOKUP@`, `@CLUSTER@`, `@SHARDS@`,
  `@TCP_PORT@`). Default `member.json.tpl`; the others are listed under
  [Joining a public network](#joining-a-public-network).
- `args` (logosdeliverynode) — the node's behaviour flags. The harness sets
  identity, network, metrics and bootstrap flags itself (`--cluster-id`,
  `--shard`, `--nat`, `--tcp-port`, `--nodekey`, `--metrics-server*`,
  `--kad-bootstrap-node`) and rejects them in `args`. Two exceptions, for a
  group joining a public network ([below](#joining-a-public-network)):
  `--kad-bootstrap-node` is allowed with `"bootstraps": []`, and `--preset`
  makes the harness leave out `--cluster-id`.
- `clusterId` — the group's cluster. Default: `network.clusterId`. A member
  must be on its bootstraps' cluster, and every bootstrap on the first one's.
  Not allowed on a logosdeliverynode group with `--preset`: the preset names
  the cluster. Shards are not per group: `network.shardsInNetwork` holds for
  every node.
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
  out round-robin across the fleet: one per member. Default: every bootstrap
  group. A delivery-module member hands libp2p its whole list (the trace shows
  `libp2p createNode OK bootstrapNodes=N`); older logos-delivery-module refs
  passed only the first. `[]` joins no
  harness bootstrap: the group's profile or args name the network. A manifest
  needs no bootstrap group if every member group says `[]`.
- `joinAll` (members, default `false`) — each member joins through every group
  in `bootstraps`, in that order, instead of one round-robin.
- `extraBootstraps` (members) — `/p2p/` multiaddrs put in front of the group's
  bootstrap list, e.g. a peer that is never up
  (`/ip4/10.0.0.99/tcp/45000/p2p/<id>` is in the reserved range, so nothing
  answers there). A module member gets them only through a profile with
  `@BOOTSTRAPS@`; the public-network profiles have none.
- `jitter` — random 0..n second spread within a delivery-module group. N daemons
  loading plugins at the same instant trip logos-core's 10 s plugin-load timeout.
- `env` — extra environment for the group's delivery-module, delivery-demo and
  sd-agent containers (a logosdeliverynode takes none). The rest
  is derived (`BOOTSTRAP_ADDR`, `CLUSTER_ID`, `NUM_SHARDS`, ports), but these are
  only reachable here: `LOOKUP_INTERVAL` (seconds, into the rendered profile),
  `MESH_CONTENT_TOPIC` (what the node subscribes to, default
  `/sim/1/mesh/proto`), `P2P_MAX_CONNS` (the plugin host's connection limits,
  default 100), `LD_DISCO_TRACE` (where the plugin writes its trace),
  `START_TIMEOUT` (seconds to wait for the node's `nodeStarted` event, default
  60). The traffic, mix and RLN variables are in the sections below.
- `expect` (sender groups) — what the group's sends should come to:
  `delivered` (default), `error` or `any` ([below](#traffic-and-what-is-expected-of-it)).

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

### Traffic and what is expected of it

`SEND_COUNT` in a `delivery-module` group's `env` makes its nodes senders.

| `env` | Default | Meaning |
|---|---|---|
| `SEND_COUNT` | `0` (not a sender) | messages each node sends on the mesh content topic |
| `SEND_AFTER` | `60` | seconds after the node subscribed before the first send |
| `SEND_INTERVAL` | `5` | seconds between sends |
| `SEND_SIZE` | payload `<node>-<n>` | pad the payload to this many bytes |
| `ANONYMITY_LEVEL` | `Preferred` in the mix profiles | `None`, `Preferred` or `Required` |

Every delivery-module node records its delivery_module events with a local
nanosecond stamp, and `collect` writes `delivery.txt` (`lib/msg_report.py`):
per message, whether the send was accepted, propagated, confirmed, and received
by every other delivery-module node; latency from send to propagated and from
send to received; and, from 50 sends up, percentiles, 5-minute steps, mix
routes and resource use.

The report ends in `verdict: PASS` or `FAIL`, judged against the group's
`expect`:

| `expect` | A message is as expected when |
|---|---|
| `delivered` (default) | the send was accepted, did not end in `messageError`, and every other delivery-module node received it |
| `error` | the send was refused or ended in `messageError`, and no node received it |
| `any` | always (the group is not judged) |

A send still without an outcome when the run ended (made in its last 60 s) is
counted as in flight and not judged.

A plain send is confirmed against a store node, so the fleet needs one
(`examples/msg-smoke.json` runs it on the bootstrap) or every send ends in
`messageError` although it was delivered.

### Mix

A mix node is a bootstrap-role group with a fixed mix key, so that its address,
identity and mix public key are known before it starts:

- native: `"kind": "logosdeliverynode"` with `--mix=true --mixkey=<64 hex>`
  (and `--lightpush=true`, since a mix exit publishes through lightpush);
- module: `"kind": "delivery-module"`, `"config": "mixnode.json.tpl"`, and
  `NODEKEY` / `MIXKEY` in `env`.

Every delivery-module node is handed all of them as `MIXNODES`, the static pool
behind a profile's `@MIXNODES@` (`member-mix-static.json.tpl`), so a sender can
build a path without waiting for discovery. `member-mix.json.tpl` has no static
pool and learns mix nodes from discovery alone. `MIX_ALLOW_ALL=1` sets
`mix-allow-all-addresses` in the mix profiles, for a logos-delivery that
restricts mix to public addresses.

For mix runs `collect` adds `mixlog.txt` (per message: the node that was its
exit, and each try of the sender), `q_mixload.json` (packets received,
forwarded and failed per node) and `q_mixpool.json` (the usable pool per node
since T0); `delivery.txt` summarises them as exit distribution, tries per
message and per-node load.

Two things to know when reading a mix result:

- A delivery-module node that mounts mix can be picked as the exit of another
  sender's message. On logos-delivery master `39b5f4ee` it then gets no
  `messageReceived` for that message, and `delivery.txt` counts the miss.
- A sender with a mix profile mounts mix too, so it serves as hop and exit for
  the other senders.

### RLN on a local LEZ chain

A manifest with an `"rln"` section runs a local LEZ chain next to the fleet and
deploys an RLN registry on it; without the section nothing changes. A
`delivery-module` group takes part with `RLN=1` in its `env` (a
`logosdeliverynode` cannot: it has no LEZ RLN backend).

```json
"rln": {},
"groups": [
  { "name": "members", "role": "member", "count": 2, "env": { "RLN": "1" } }
]
```

| Key / env | Default | Meaning |
|---|---|---|
| `rln.epochSizeSec`, `rln.maxEpochGap` | `10`, `3` | The RLN epoch and window of every node's preset. These are the Mix profile's; Mix RLN refuses to start on others. |
| `rln.sequencerImage` | `logos-sim-lez-sequencer:local` | The standalone LEZ sequencer. The default is built by `harness.sh build`. |
| `RLN=1` | off | Relay RLN: the node validates proofs, registers a membership and attaches proofs to what it sends. |
| `RLN_MEMBERSHIP=0` | `1` | No membership: the node validates, and its sends end in `messageError`. |
| `RLN_VALIDATE=0` | `1` | Attach proofs without validating incoming ones. |
| `MIX_RLN=1` | off | Per-hop Mix proofs from the same registry (needs `RLN=1` and a flat-shape profile: `mixnode.json.tpl`, `member-mix-static.json.tpl`). |

What runs: `lez-init` mints the payer the chain funds at genesis, `lez-sequencer`
produces blocks, `lez-setup` deploys the registry and then funds each node's LEZ
payer on request (`docker/lez.py`). A node with `RLN=1` waits for the registry,
reaches RLN `Ready`, is funded, and starts once its membership is `active` --
about a minute after T0 for the first nodes, so give `SEND_AFTER` no less than 30.

`harness.sh build` with such a manifest also builds the two RLN modules, the
sequencer (from the logos-execution-zone source, tag `LEZ_REF`) and
logos-lez-rln's tools. The first build takes over an hour; the results are kept
(`out/lez-tools`, the images, the `logos-harness-lez-build` volume). Give an RLN
manifest an `image` of its own: only an image built from such a manifest holds
the RLN modules.

A mix node as a `delivery-module` node is a bootstrap-role group with
`"config": "mixnode.json.tpl"` and `NODEKEY` / `MIXKEY` in its `env`; like the
native ones with `--mixkey`, it is handed to the other module nodes as `MIXNODES`.

- `examples/rln-smoke.json`: a sender with a membership (all received) and one
  without, whose group says `"expect": "error"` (every send a `messageError`).
- `examples/mix-rln-smoke.json`: mix over the RLN network, 4 module mix nodes.
  On master `39b5f4ee` each message is missing at one mix node, its exit (see [Mix](#mix)).

In `delivery.txt`, "receptions of other messages" are Mix RLN coordination
frames, which every node relays.

### Joining a public network

Members can join logos.dev or logos.test instead of, or alongside, a local
bootstrap. Give the group `"bootstraps": []` and let the network preset do
the rest: its entry nodes become the node's kad bootstraps.

```json
{ "name": "test-mod", "role": "member", "count": 3, "bootstraps": [],
  "config": "member-logostest.json.tpl" }

{ "name": "test-native", "role": "member", "kind": "logosdeliverynode", "count": 3,
  "bootstraps": [], "args": ["--preset=logos.test", "--entry-layer=kernel", "..."] }
```

| Profile in `conf/` | Network | Discovery |
|---|---|---|
| `member.json.tpl` (default) | the harness's: `@CLUSTER@`, `@SHARDS@`, `@BOOTSTRAPS@` | kad on libp2p_module (plugin) |
| `member-mix.json.tpl` | the harness's | plugin kad, `anonymityLevel` from `env.ANONYMITY_LEVEL` (default `Preferred`); mix nodes come from discovery only |
| `member-mix-static.json.tpl` | the harness's | plugin kad, `anonymityLevel` from `env.ANONYMITY_LEVEL` (default `Preferred`), static mix pool from the manifest's fixed-key mix nodes. Uses the legacy flat config shape: `messagingOverrides` has no `mixnode` option |
| `member-logosdev.json.tpl` | logos.dev | kad on libp2p_module (plugin) |
| `member-logosdev-kad.json.tpl` | logos.dev | kad in-process |
| `member-logosdev-mix.json.tpl` | logos.dev | plugin kad, `anonymityLevel: Preferred` |
| `member-logostest.json.tpl` | logos.test | kad on libp2p_module (plugin) |

A logosdeliverynode takes `--preset=<network>` in `args`, and the harness then
passes no `--cluster-id`. An explicit cluster id would override the preset's,
and `--cluster-id=2` even replaces any preset with logos.dev. The other harness
flags still apply, `--shard=0` included. The containers reach the network
through docker's NAT and advertise their private address (`--nat=extip:`), so
they dial out but cannot be dialled from it.

### sd-agent nodes

An `sd-agent` group runs libp2p_module alone — kad-dht and service discovery,
no delivery node — and drives it through the module's public API
(`docker/sd_agent.py`). It can be a bootstrap or a member, and it joins like a
module node. `{i}` in a value is the node's 1-based index in its group.

| `env` | Meaning |
|---|---|
| `SD_ADVERTISE` | comma list of service ids, advertised once at start |
| `SD_FLAP` | `<svc>:<on_s>:<off_s>`: advertise, stop, repeat |
| `SD_LOOKUP` | comma list looked up every cycle |
| `SD_LOOKUP_ROTATE` | comma list looked up in rotation; `prefix[a-b]` expands |
| `SD_LOOKUP_PER_CYCLE`, `SD_LOOKUP_INTERVAL`, `SD_LOOKUP_START` | rotating ids per cycle (4), seconds per cycle (20), seconds before the first (0) |
| `SD_LOOKUP_PARALLEL` | `1` (default): a cycle's lookups run concurrently; `0`: one by one |
| `P2P_MAX_CONNS` | libp2p connection limit (100) |

Each agent writes one JSON line per advertise, stop and lookup to
`<node>.sd.jsonl`, which `collect` keeps. Read a run with

```bash
python3 lib/sd_report.py <collect-dir> [window-seconds]
```

`examples/sd-smoke.json` is a 5-minute check of the driver. The branch
`experiment/sd-corner-cases` holds a campaign built on this kind.

### Demo nodes

A `delivery-demo` group runs [logos-delivery-demo](https://github.com/logos-co/logos-delivery-demo)
— the educational UI for delivery_module — as a fleet member you can watch and
use in a browser:

```json
{ "name": "demo", "role": "member", "kind": "delivery-demo", "count": 2,
  "startAfter": 0, "stopAfter": 900, "env": { "LOOKUP_INTERVAL": "15" } }
```

It takes the same fields as a delivery-module member (`count`, `startAfter`,
`stopAfter`, `manual`, `jitter`, `config`, `env`, `clusterId`, `bootstraps`,
`joinAll`, `extraBootstraps`) and joins the same way:
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

#### Watch one live

A fleet with a `manual` demo you attach by hand
([`examples/demo-manual.json`](examples/demo-manual.json)):

```bash
export HARNESS_MANIFEST=examples/demo-manual.json   # 1 bootstrap, 3 members, 1 manual demo
./harness.sh build                                  # first time only; cached after
./harness.sh up                                     # fleet up, the demo left off
./harness.sh start demo                             # attach it; prints its noVNC URL
open http://localhost:6080/vnc.html                 # Connect: the demo UI, live
tail -f out/traces/m4.trace                         # its discovery lookups as they happen
./harness.sh stop demo                              # detach; start again whenever
./harness.sh down -v                                # tear everything down
```

The group name becomes a Prometheus label, so dashboards can compare late joiners
against steady-state nodes without hand-written queries.

<a id="members-are-scattered"></a>Members are scattered across the subnet on purpose: the nim-libp2p registrar
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
| `harness.sh run [sec] [dir]` | `up`, wait until T0 + `sec` (default: the manifest's `duration`), `collect` into `dir` (default `out/collect/<utc-stamp>`), `down -v`. Launches or stops scheduled at or after the end are dropped, with a warning. Samples every container's CPU and memory every 30 s |
| `harness.sh suite [--build] <manifest>…` | `run` each manifest for its `duration`, one after another, into `out/collect/suite-<utc-stamp>/<name>`; print and keep one verdict line each (`summary.txt`). `--build` first builds the images that are missing |
| `harness.sh collect [dir]` | snapshot a running stack: stage log, container states, traces, events, plan and pins, metrics, mix routes, reports, and the logs of every bootstrap and logosdeliverynode and of any delivery-module member with an error, a failed stage or a stopped container (`COLLECT_LOGS=all` keeps every log). See [Results](#results) |
| `harness.sh start <group\|node>…` | start groups or nodes in the running stack — a `manual` group, or any other mid-run. Refuses when the bootstrap they join through is not running; logs to `run.log` and prints a demo's noVNC URL |
| `harness.sh stop <group\|node>…` | stop them again (graceful, as for `stopAfter`); `start` brings them back |
| `harness.sh down [-v]` | tear down |
| `harness.sh report [dir] [n]` | discovery report over a trace dir (default `out/traces`); `n` nodes expected in the DHT, default from the plan |
| `promq.sh '<promql>'` | an instant Prometheus query against the running stack, from inside its network (`--raw <path>` for any API path) |

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
| `IMAGE` | the manifest's `image` | the runtime image `build` produces and the nodes run |
| `LEZ_REF` | `v0.3.2` | the logos-execution-zone tag the RLN sequencer is built from |
| `COLLECT_LOGS` | `flagged` | `all` keeps every container's log at `collect` |
| `PYTHON` | `python3` | the interpreter for `lib/` |
| `SUBNET` | `10.0.0.0/8` | the compose network, read by `docker-compose.yml` |
| `PROMETHEUS_PORT` | `9090` | published Prometheus port |
| `GRAFANA_PORT` | `3000` | published Grafana port |

`NIX_VOLUME` is worth knowing about: it is where every build is cached, so two
checkouts sharing it rebuild almost nothing, and `docker volume rm` on it is
how you force a cold build.

## Results

What `collect` (and so `run`) leaves in the directory:

| File | Holds |
|---|---|
| `run.log`, `plan.json`, `resolved.json`, `t0` | T0 and every launch and stop with its offset; the groups and their nodes; the exact revision of every component |
| `ps.txt`, `stages.txt` | container states; each module node's start-up stages |
| `report.txt`, `*.trace` | the discovery report and the traces it is made from |
| `delivery.txt`, `*.send.jsonl`, `*.msg.jsonl` | the delivery report with its verdict; every send call and every delivery_module event per node |
| `mixlog.txt`, `q_mixload.json`, `q_mixpool.json` | mix exits and tries per message; per-node mix counters; the usable pool since T0 |
| `stats.tsv` | CPU and memory per container every 30 s (`run` only) |
| `q_up.json`, `q_peers.json`, `q_relay.json` | Prometheus at collection |
| `*.sd.jsonl` | sd-agent events |
| `*.log`, `mN.png` | kept container logs; a screenshot of each demo |
| `warnings.txt` | present only when something could not be collected, e.g. a node whose log did not come back for the route data |

[AGENTS.md](AGENTS.md) describes how to read each of them.

## Layout

```
harness.sh           CLI
promq.sh             Prometheus queries against the running stack
harness.json         default manifest
docker-compose.yml   network, Prometheus, Grafana (nodes come from out/compose.groups.yml)
lib/                 manifest parsing, lock resolution, generators
docker/              member and demo images: Dockerfiles, entrypoints, profile renderer, QML inspector client
conf/                node profiles
monitoring/          Prometheus config, Grafana provisioning + dashboards
examples/            one ready-made manifest per capability
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

**"Set up a small experiment I can drive myself: one native bootstrap, three
module members and two demo UIs that I attach by hand. Build it and bring the
fleet up, but leave the demos off, and tell me how to start them."** — the agent
writes a manifest like [`examples/demo-manual.json`](examples/demo-manual.json)
with `"count": 2` on the manual demo group into `out/manifests/`, builds, runs
`up`, and hands back the two commands and URLs:

```bash
./harness.sh start demo      # both demos; noVNC on http://localhost:6080 and :6081
./harness.sh stop m5         # or one node at a time, by name
```

**"Run the smokes and tell me which pass."** — a suite; each manifest names its
own build:

```bash
./harness.sh suite --build examples/msg-smoke.json examples/sd-smoke.json \
  examples/mix-static-smoke.json examples/rln-smoke.json
```

**"Did anyone lose libp2p?"** — after a run, `grep 'module lost' <dir>/stages.txt`;
`collect` kept that member's log.
