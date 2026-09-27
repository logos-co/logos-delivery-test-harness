"""Generate the compose override, the Prometheus target list and the launch plan.

One compose service per node. A group's `role` (bootstrap or member) says what
the node is for; its `kind` says what runs: `logosdeliverynode`, the delivery
binary with in-process kademlia, or `delivery-module`, a logoscore daemon
hosting delivery_module with discovery on libp2p_module.

Members are scattered across the subnet because the nim-libp2p registrar scores
an advertiser by how many address-prefix bits it shares with the ads it already
holds (iptree.ipScore), and every shared bit costs advertExpiry/32 of waiting --
sequential addresses in one /24 share ~27 bits and cost 13-15 minutes per
registration.
"""
import ipaddress
import json
import os
import sys

import manifest
from manifest import (
    DEMO_NODE,
    DEMO_VNC_PORT,
    MODULE_METRICS_PORT,
    MODULE_P2P_PORT,
    MODULE_TCP_PORT,
    NATIVE,
    NATIVE_METRICS_PORT,
)


def _addresses(net, count, taken):
    """Deterministic scatter over the subnet, skipping the infra range."""
    span = net.num_addresses - manifest.INFRA_HOSTS - 2
    out = []
    for i in range(1, count + 1):
        h = (i * 2654435761) & 0xFFFFFFFF
        ip = net[manifest.INFRA_HOSTS + h % span]
        while ip in taken:
            ip += 1
        taken.add(ip)
        out.append(str(ip))
    return out


def _depends(on):
    if not on:
        return ""
    return f"""    depends_on:
      {on}:
        condition: service_healthy
"""


def _native_service(g, name, ip, port, net_conf, join, depends_on):
    """logosdeliverynode, as a bootstrap or a member. `join` is the bootstrap
    address it joins the DHT through, empty for the first bootstrap."""
    args = [
        "logosdeliverynode",
        f"--cluster-id={net_conf['clusterId']}",
        "--shard=0",
        f"--nat=extip:{ip}",
        f"--tcp-port={port}",
        "--metrics-server=true",
        "--metrics-server-address=0.0.0.0",
        f"--metrics-server-port={NATIVE_METRICS_PORT}",
    ]
    if "nodekey" in g:
        args.append(f"--nodekey={g['nodekey']}")
    args += list(g["args"])
    if join:
        args.append(f"--kad-bootstrap-node={join}")
    cmd = "\n".join(f"      - {a}" for a in args)
    return f"""  {name}:
    image: logos-sim:local
    hostname: {name}
    labels:
      harness.group: "{g['name']}"
      harness.role: "{g['role']}"
      harness.kind: "{g['kind']}"
    environment:
      # The nix-built binary dlopens libpq at run time; take the one the
      # portable delivery_module bundle ships (Ubuntu's would not resolve
      # through the nix loader).
      LD_LIBRARY_PATH: /opt/modules/delivery_module
    command:
{cmd}
{_depends(depends_on)}    networks:
      sim:
        ipv4_address: {ip}
    healthcheck:
      test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/{port}"]
      interval: 2s
      timeout: 2s
      retries: 30
      start_period: 3s
"""


def _module_service(g, name, ip, net_conf, join, depends_on):
    """A logoscore daemon hosting delivery_module, as a bootstrap or a member.
    A bootstrap carries a fixed libp2p key, so its DHT address is known ahead."""
    env = {
        "BOOTSTRAP_ADDR": join,
        "MEMBER_CONFIG": g["config"],
        "CLUSTER_ID": str(net_conf["clusterId"]),
        "NUM_SHARDS": str(net_conf["shardsInNetwork"]),
        "START_JITTER": str(g["jitter"]),
        "P2P_PORT": str(MODULE_P2P_PORT),
        "TCP_PORT": str(MODULE_TCP_PORT),
        "METRICS_PORT": str(MODULE_METRICS_PORT),
    }
    if g["role"] == "bootstrap":
        env["LIBP2P_PRIVKEY"] = manifest.libp2p_privkey(g)
    env.update({k: str(v) for k, v in g["env"].items()})
    envblock = "\n".join(f"      {k}: \"{v}\"" for k, v in env.items())
    health = ""
    if g["role"] == "bootstrap":
        # Members may join once the DHT port answers; libp2p binds the
        # container address, not loopback.
        health = f"""    healthcheck:
      test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/$$(hostname -i | cut -d' ' -f1)/{MODULE_P2P_PORT}"]
      interval: 2s
      timeout: 2s
      retries: 60
      start_period: 5s
"""
    return f"""  {name}:
    image: logos-sim:local
    hostname: {name}
    labels:
      harness.group: "{g['name']}"
      harness.role: "{g['role']}"
      harness.kind: "{g['kind']}"
    entrypoint: ["/opt/sim/entrypoint-module.sh"]
    environment:
{envblock}
{_depends(depends_on)}    volumes:
      - ./out/traces:/traces
    networks:
      sim:
        ipv4_address: {ip}
{health}"""


def _demo_service(g, name, ip, net_conf, join, depends_on, paths, vnc_port):
    """The logos-delivery-demo UI as a member: joins like a module member, and
    its screen is published on `vnc_port` through noVNC. The app runs from the
    nix volume; `paths` are its store paths (out/demo-paths.json)."""
    env = {
        "BOOTSTRAP_ADDR": join,
        "MEMBER_CONFIG": g["config"],
        "CLUSTER_ID": str(net_conf["clusterId"]),
        "NUM_SHARDS": str(net_conf["shardsInNetwork"]),
        "START_JITTER": str(g["jitter"]),
        "P2P_PORT": str(MODULE_P2P_PORT),
        "TCP_PORT": str(MODULE_TCP_PORT),
        "DEMO_APP": paths["app"],
        "DEMO_PLUGIN": paths["plugin"],
        "DEMO_MODULES": paths["modules"],
        "DEMO_LIBP2P_MODULE": paths["libp2pModule"],
    }
    env.update({k: str(v) for k, v in g["env"].items()})
    envblock = "\n".join(f"      {k}: \"{v}\"" for k, v in env.items())
    return f"""  {name}:
    image: logos-sim-demo:local
    hostname: {name}
    labels:
      harness.group: "{g['name']}"
      harness.role: "{g['role']}"
      harness.kind: "{g['kind']}"
    entrypoint: ["/opt/sim/entrypoint-demo.sh"]
    environment:
{envblock}
{_depends(depends_on)}    volumes:
      - nixstore:/nix:ro
      - ./out/traces:/traces
    ports:
      - "{vnc_port}:{DEMO_VNC_PORT}"
    networks:
      sim:
        ipv4_address: {ip}
"""


def _demo_paths(compose_path):
    path = os.path.join(os.path.dirname(os.path.abspath(compose_path)), "demo-paths.json")
    if not os.path.exists(path):
        sys.exit(
            f"harness: the manifest runs {DEMO_NODE} nodes but {path} is missing -- "
            "run ./harness.sh build with this manifest first"
        )
    with open(path) as f:
        return json.load(f)


def _target(g, name, ip):
    port = NATIVE_METRICS_PORT if g["kind"] == NATIVE else MODULE_METRICS_PORT
    return {
        "targets": [f"{ip}:{port}"],
        "labels": {"node": name, "role": g["role"], "kind": g["kind"], "group": g["name"]},
    }


def generate(m, compose_path, targets_path, plan_path):
    net_conf = m["network"]
    net = ipaddress.ip_network(net_conf["subnet"])

    # A bootstrap group holds exactly one node (manifest.load enforces it), and
    # its service is named after the group. Every bootstrap after the first
    # joins the first one's DHT, so they form one network, not several.
    boots = {g["name"]: g for g in m["groups"] if g["role"] == "bootstrap"}
    taken = {ipaddress.ip_address(g["ip"]) for g in boots.values()}
    first = next(iter(boots.values()))
    dht_entry = manifest.bootstrap_addr(first)

    services, targets, plan = [], [], []
    member_no = 0
    joins = {}  # member service -> the bootstrap service it joins through
    demo_paths = _demo_paths(compose_path) if manifest.uses_demo(m) else None
    vnc = {}

    for g in m["groups"]:
        names = []
        if g["role"] == "bootstrap":
            name, ip = g["name"], g["ip"]
            join, dep = ("", "") if g is first else (dht_entry, first["name"])
            if g["kind"] == NATIVE:
                services.append(_native_service(g, name, ip, g["port"], net_conf, join, dep))
            else:
                services.append(_module_service(g, name, ip, net_conf, join, dep))
            targets.append(_target(g, name, ip))
            names.append(name)
        else:
            for ip in _addresses(net, g["count"], taken):
                member_no += 1
                name = f"m{member_no}"
                # Round-robin over the whole fleet, so late joiners alternate too.
                boot = boots[g["bootstraps"][(member_no - 1) % len(g["bootstraps"])]]
                join = manifest.bootstrap_addr(boot)
                joins[name] = boot["name"]
                if g["kind"] == NATIVE:
                    services.append(
                        _native_service(g, name, ip, MODULE_TCP_PORT, net_conf, join, boot["name"])
                    )
                elif g["kind"] == DEMO_NODE:
                    # No Prometheus target: no CLI into the demo's core to start
                    # openmetrics with.
                    vnc[name] = DEMO_VNC_PORT + len(vnc)
                    services.append(
                        _demo_service(g, name, ip, net_conf, join, boot["name"],
                                      demo_paths, vnc[name])
                    )
                    names.append(name)
                    continue
                else:
                    services.append(_module_service(g, name, ip, net_conf, join, boot["name"]))
                targets.append(_target(g, name, ip))
                names.append(name)
        step = {
            "name": g["name"],
            "role": g["role"],
            "kind": g["kind"],
            "startAfter": g["startAfter"],
            "services": names,
        }
        if "stopAfter" in g:
            step["stopAfter"] = g["stopAfter"]
        if g["kind"] == DEMO_NODE:
            step["vnc"] = {n: vnc[n] for n in names}
        if g["manual"]:
            step["manual"] = True
        if g["role"] == "member":
            step["joins"] = {n: joins[n] for n in names}
        plan.append(step)

    with open(compose_path, "w") as f:
        f.write("# generated by harness gen -- do not edit\n")
        f.write("services:\n")
        f.write("\n".join(services))
        if demo_paths:
            # The demo's closure is read from the builder's store volume.
            nix_volume = os.environ.get("NIX_VOLUME", "logos-harness-nix")
            f.write(f"\nvolumes:\n  nixstore:\n    external: true\n    name: {nix_volume}\n")

    with open(targets_path, "w") as f:
        json.dump(targets, f, indent=1)
        f.write("\n")

    with open(plan_path, "w") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")

    return plan


if __name__ == "__main__":
    m = manifest.load(sys.argv[1])
    plan = generate(m, sys.argv[2], sys.argv[3], sys.argv[4])
    total = sum(len(g["services"]) for g in plan)
    print(f"{total} nodes in {len(plan)} groups")
    for g in plan:
        stop = f"  stop at +{g['stopAfter']}s" if "stopAfter" in g else ""
        when = " manual" if g.get("manual") else f"+{g['startAfter']:>5}s"
        print(
            f"  {when}  {g['name']:<16} {g['role']:<9} {g['kind']:<17} "
            f"{len(g['services'])} node(s){stop}"
        )
        for n, p in g.get("vnc", {}).items():
            print(f"           {n}: http://localhost:{p}/vnc.html")
