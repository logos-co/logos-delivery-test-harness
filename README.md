# logos-delivery-test-harness

A container fleet for exercising logos-delivery peer discovery: a seed, any number
of plugin-hosted member nodes, Prometheus and provisioned Grafana dashboards.

Everything is built by nix from named git revisions, so a manifest plus the
generated `out/resolved.json` reproduces an image exactly.

## Requirements

Docker, bash, python3. **Nix is not needed on the host** — it runs inside the
builder container, whose store persists in the `logos-harness-nix` volume.

## Quickstart

```bash
./harness.sh build          # resolve, nix-build every component, build logos-sim:local
./harness.sh up             # launch the groups on their schedule
open http://localhost:3000    # Grafana (anonymous admin)
./harness.sh down -v
```

`HARNESS_MANIFEST=examples/37-node-late-joiners.json ./harness.sh up` runs the
37-node late-joiner topology instead of the small default.

## The manifest

`harness.json` pins exactly **one** thing — the logos-delivery-module ref — and
declares the node groups. Every other component is derived from that module's own
flake lock, so the harness always builds a coherent set:

| Component | Where its revision comes from |
|---|---|
| `logos-delivery-module` | `module.ref` in the manifest (branch, tag or `rev`) |
| `logos-delivery` (the seed binary) | the module's lock, on every ref |
| `libp2p_module` | the module's lock **where the ref pins it**, else `overrides` |
| `openmetrics-module` | as above |
| `logos-logoscore-cli` | as above |

**What a ref pins differs by ref.** `master` of logos-delivery-module pins only
`logos-delivery` — the other three arrived with the plugin-discovery work and
exist on those branches. So the shipped manifest pins them itself:

```json
"overrides": {
  "libp2p_module": { "flake": "git+https://github.com/logos-co/logos-libp2p-module",
                     "rev": "ec7b8f58..." }
}
```

An override carrying a full `flake` stands alone and needs no lock entry; one
carrying only `rev` swaps the revision of a component the module does pin.
Point `module.ref` at a branch that pins all five and you can delete the
overrides entirely — `harness.sh resolve` will say so by reporting each component's
source as `module-lock` instead of `override`.

One of them is a standing decision rather than a gap: the module's discovery
branches lock `logos-logoscore-cli` at `454e0696`, while the harness has always
run `665ac28b`. The override keeps that behaviour; drop it to follow the module.

### Groups

A group is *n nodes sharing one profile, launched at one offset*:

```json
{ "name": "late-5min", "kind": "member", "count": 1,
  "startAfter": 300, "jitter": 0, "config": "member.json.tpl" }
```

- `kind` — `seed` (native `logosdeliverynode`, in-process kademlia) or `member`
  (logos-core daemon hosting delivery_module + libp2p_module).
- `count` + `config` — n nodes with profile x, m nodes with profile y. Profiles
  live in `conf/` and are rendered per node (`@IP@`, `@SEED@`, `@LOOKUP@`,
  `@CLUSTER@`, `@SHARDS@`, `@TCP_PORT@`).
- `startAfter` — seconds from T0, where T0 is when the offset-0 groups launch.
- `jitter` — random 0..n second spread within the group. N daemons loading
  plugins at the same instant trip logos-core's 10 s plugin-load timeout.
- `env` — extra environment for the group's containers, e.g. `LOOKUP_INTERVAL`.

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
| `harness.sh up` | gen, start monitoring, launch each group at its offset, log to `out/run.log` |
| `harness.sh down [-v]` | tear down |
| `harness.sh report` | discovery report over the traces in `out/traces` |

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
and `out/run.log` records T0 and each group's actual launch time. Keep both
alongside any measurements taken from a run.
