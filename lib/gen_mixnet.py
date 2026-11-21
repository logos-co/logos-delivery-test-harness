#!/usr/bin/env python3
"""Write a manifest for a mix network of a given size.

  gen_mixnet.py [options] > out/manifests/<name>.json

One bootstrap (kad entry, store), --mix fixed-key mix nodes (relay, lightpush,
mix), --senders delivery-module nodes sending with anonymityLevel Required, and
--receivers plain members. Each mix node is its own bootstrap-role group: it has
a fixed address and keys, so every module node is handed all of them as its
pool (MIXNODES) and sends from the start, without waiting for discovery.

  --mix-kind native|module   logosdeliverynode (default) or delivery-module mix nodes
  --allow-all                mix-allow-all-addresses, for a logos-delivery that
                             restricts mix to public addresses (#4631, #4729)
  --rln                      local LEZ chain; Relay RLN on every module node and
                             Mix RLN on the mix nodes and senders (needs module mix nodes)
  --stop N --stop-at S       the last N mix nodes are stopped S seconds from T0
                             (`harness.sh start <group>...` brings them back)
  --count C --interval I --send-after A   per sender: C messages, one every I s
  --delivery-rev SHA         build logos-delivery at this commit (overrides.logos-delivery)
  --delivery-flake URL       ... from this flake instead of the module's (a fork)
  --libp2p-module-rev SHA    build libp2p_module at this commit (overrides.libp2p_module)
  --libp2p-module-flake URL  ... from this flake
  --discover                 no static pool: module mix nodes and senders learn the
                             mix nodes from service discovery (needs --mix-kind module)
  --image NAME               the image of that build, e.g. logos-sim:pr4647
  --duration S               how long `run` and `suite` let it run
  --base MANIFEST            take module, overrides, rln, network, the logosdeliverynode
                             bootstrap and the rln-allocator groups from MANIFEST, and
                             its first mix node, "receivers" and "senders" groups as
                             templates (their env, config, jitter); --mix-kind, --rln,
                             --allow-all and --discover then come from the base
  --allocator-funding N      what the rln-allocator's payer is funded with (with --base)
  --spread                   network.spread: mix nodes and members get the addresses
                             with the shortest shared prefixes (manifest.spread_addresses)
"""
import argparse
import copy
import hashlib
import ipaddress
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest  # noqa: E402

N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def key(label):
    """A deterministic, valid secp256k1 / x25519 private key."""
    return f"{int(hashlib.sha256(label.encode()).hexdigest(), 16) % (N - 1) + 1:064x}"


def mix_ips(a, network, boot):
    """A registrar makes an advertiser wait for every address bit it shares
    with the ads it already holds: one /16 each, or with --spread the
    addresses sharing the fewest leading bits."""
    if a.spread:
        return manifest.spread_addresses(ipaddress.ip_network(network["subnet"]), a.mix,
                                         {ipaddress.ip_address(boot["ip"])})
    return [f"10.{i * 9 % 250 + 1}.{i}.20" for i in range(1, a.mix + 1)]


def from_base(a):
    """--base: the run's build, chain and network from a base manifest, its node
    groups resized from its own templates."""
    with open(a.base) as f:
        b = json.load(f)
    groups = b["groups"]
    boot = next(g for g in groups if g["role"] == "bootstrap" and g.get("kind") == "logosdeliverynode")
    mix_t = next(g for g in groups if g.get("config") == "mixnode.json.tpl")
    recv_t = next(g for g in groups if g["name"] == "receivers")
    send_t = next(g for g in groups if g["name"] == "senders")
    network = dict(b["network"])
    if a.spread:
        network["spread"] = True
    ips = mix_ips(a, network, boot)

    out_groups = [copy.deepcopy(boot)]
    for i in range(1, a.mix + 1):
        g = copy.deepcopy(mix_t)
        g.update(name=f"mix{i}", ip=ips[i - 1], nodekey=key(f"dht{i}"))
        g["env"].update(NODEKEY=key(f"node{i}"), MIXKEY=key(f"mixkey{i}"))
        if i > a.mix - a.stop:
            g["stopAfter"] = a.stop_at
        out_groups.append(g)
    recv = copy.deepcopy(recv_t)
    recv["count"] = a.receivers
    send = copy.deepcopy(send_t)
    send["count"] = a.senders
    send["env"].update(SEND_COUNT=str(a.count), SEND_INTERVAL=str(a.interval),
                       SEND_AFTER=str(a.send_after))
    out_groups += [recv, send]
    for g in groups:
        if g.get("kind") == "rln-allocator":
            g = copy.deepcopy(g)
            if a.allocator_funding:
                g.setdefault("env", {})["ALLOCATOR_FUNDING"] = str(a.allocator_funding)
            out_groups.append(g)

    out = {"_comment": ["generated: lib/gen_mixnet.py " + " ".join(sys.argv[1:])]}
    for k in ("image", "duration"):
        v = getattr(a, k) or b.get(k)
        if v:
            out[k] = v
    out.update(module=b["module"], overrides=b["overrides"])
    if "rln" in b:
        out["rln"] = b["rln"]
    out["network"] = network
    out["groups"] = out_groups
    json.dump(out, sys.stdout, indent=2)
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mix", type=int, default=25)
    ap.add_argument("--senders", type=int, default=8)
    ap.add_argument("--receivers", type=int, default=12)
    ap.add_argument("--mix-kind", choices=("native", "module"), default="native")
    ap.add_argument("--allow-all", action="store_true")
    ap.add_argument("--rln", action="store_true")
    ap.add_argument("--stop", type=int, default=0)
    ap.add_argument("--stop-at", type=int, default=1200)
    ap.add_argument("--count", type=int, default=700)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--send-after", type=int, default=120)
    ap.add_argument("--delivery-rev")
    ap.add_argument("--delivery-flake")
    ap.add_argument("--libp2p-module-flake")
    ap.add_argument("--libp2p-module-rev")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--image")
    ap.add_argument("--duration", type=int)
    ap.add_argument("--base")
    ap.add_argument("--allocator-funding", type=int)
    ap.add_argument("--spread", action="store_true")
    a = ap.parse_args()
    if a.base:
        return from_base(a)
    if a.discover and a.mix_kind != "module":
        ap.error("--discover needs --mix-kind module")
    if a.rln and a.mix_kind != "module":
        ap.error("--rln needs --mix-kind module: a logosdeliverynode has no LEZ RLN backend")

    with open(os.path.join(os.path.dirname(__file__), "..", "harness.json")) as f:
        base = json.load(f)
    boot = base["groups"][0]
    boot["args"] += ["--store=true", "--store-message-db-url=sqlite:///tmp/store.sqlite3"]
    rln_env = {"RLN": "1"} if a.rln else {}
    mix_rln_env = {**rln_env, "MIX_RLN": "1"} if a.rln else {}
    allow_env = {"MIX_ALLOW_ALL": "1"} if a.allow_all else {}
    # An empty pool of its own keeps a node from being handed the manifest's.
    pool_env = {"MIXNODES": ""} if a.discover else {}

    ips = mix_ips(a, base["network"], boot)
    if a.spread:
        base["network"]["spread"] = True
    groups = [boot]
    for i in range(1, a.mix + 1):
        g = {"name": f"mix{i}", "role": "bootstrap", "count": 1, "startAfter": 0,
             "ip": ips[i - 1]}
        if a.mix_kind == "native":
            g.update(kind="logosdeliverynode", nodekey=key(f"node{i}"), args=[
                x for x in boot["args"] if not x.startswith("--store")
            ] + ["--lightpush=true", "--mix=true", f"--mixkey={key(f'mixkey{i}')}"]
              + (["--mix-allow-all-addresses=true"] if a.allow_all else []))
        else:
            g.update(kind="delivery-module", nodekey=key(f"dht{i}"), config="mixnode.json.tpl",
                     env={"LOOKUP_INTERVAL": "15", "NODEKEY": key(f"node{i}"),
                          "MIXKEY": key(f"mixkey{i}"), **allow_env, **mix_rln_env, **pool_env})
        if i > a.mix - a.stop:
            g["stopAfter"] = a.stop_at
        groups.append(g)
    groups.append({"name": "receivers", "role": "member", "kind": "delivery-module",
                   "count": a.receivers, "startAfter": 0, "jitter": 20,
                   "config": "member.json.tpl", "bootstraps": ["bootstrap"],
                   "env": {"LOOKUP_INTERVAL": "15", **rln_env}})
    groups.append({"name": "senders", "role": "member", "kind": "delivery-module",
                   "count": a.senders, "startAfter": 0, "jitter": 20,
                   "config": "member-mix-static.json.tpl", "bootstraps": ["bootstrap"],
                   "env": {"LOOKUP_INTERVAL": "15", "ANONYMITY_LEVEL": "Required",
                           "SEND_COUNT": str(a.count), "SEND_INTERVAL": str(a.interval),
                           "SEND_AFTER": str(a.send_after), **allow_env, **mix_rln_env, **pool_env}})

    out = {"_comment": ["generated: lib/gen_mixnet.py " + " ".join(sys.argv[1:])]}
    if a.image:
        out["image"] = a.image
    if a.duration:
        out["duration"] = a.duration
    out.update(module=base["module"], overrides=base["overrides"])
    if a.delivery_rev:
        out["overrides"]["logos-delivery"] = {"rev": a.delivery_rev}
        if a.delivery_flake:
            out["overrides"]["logos-delivery"]["flake"] = a.delivery_flake
    if a.libp2p_module_rev:
        out["overrides"]["libp2p_module"] = {"rev": a.libp2p_module_rev}
        if a.libp2p_module_flake:
            out["overrides"]["libp2p_module"]["flake"] = a.libp2p_module_flake
    if a.rln:
        out["rln"] = {}
    out["network"] = base["network"]
    out["groups"] = groups
    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
