"""Generate the compose override, the Prometheus target list and the launch plan.

One compose service per node. Members are scattered across the subnet because
the nim-libp2p registrar scores an advertiser by how many address-prefix bits it
shares with the ads it already holds (iptree.ipScore), and every shared bit costs
advertExpiry/32 of waiting -- sequential addresses in one /24 share ~27 bits and
cost 13-15 minutes per registration.
"""
import ipaddress
import json
import sys

import manifest

METRICS_PORT = 9100
SEED_METRICS_PORT = 8008
MEMBER_P2P_PORT = 45000
MEMBER_TCP_PORT = 44000


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


def _seed_service(g, net_conf, name, dht_entry):
    args = [
        "logosdeliverynode",
        f"--cluster-id={net_conf['clusterId']}",
        "--shard=0",
        f"--nat=extip:{g['ip']}",
        f"--tcp-port={g['port']}",
        f"--nodekey={g['nodekey']}",
    ] + list(g["args"])
    if dht_entry:
        # Every seed after the first joins the first one's DHT; otherwise each
        # seed and the members bootstrapping from it form a network of their own.
        args.append(f"--kad-bootstrap-node={dht_entry}")
    cmd = "\n".join(f"      - {a}" for a in args)
    return f"""  {name}:
    image: logos-sim:local
    hostname: {name}
    labels:
      harness.group: "{g['name']}"
    environment:
      # The nix-built binary dlopens libpq at run time; take the one the
      # portable delivery_module bundle ships (Ubuntu's would not resolve
      # through the nix loader).
      LD_LIBRARY_PATH: /opt/modules/delivery_module
    command:
{cmd}
    networks:
      sim:
        ipv4_address: {g['ip']}
    healthcheck:
      test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/{g['port']}"]
      interval: 2s
      timeout: 2s
      retries: 30
      start_period: 3s
"""


def _member_service(g, name, ip, seed_addr, net_conf, seed_service):
    env = {
        "SEED_ADDR": seed_addr,
        "MEMBER_CONFIG": g["config"],
        "CLUSTER_ID": str(net_conf["clusterId"]),
        "NUM_SHARDS": str(net_conf["shardsInNetwork"]),
        "START_JITTER": str(g["jitter"]),
        "P2P_PORT": str(MEMBER_P2P_PORT),
        "TCP_PORT": str(MEMBER_TCP_PORT),
        "METRICS_PORT": str(METRICS_PORT),
    }
    env.update({k: str(v) for k, v in g["env"].items()})
    envblock = "\n".join(f"      {k}: \"{v}\"" for k, v in env.items())
    depends = ""
    if seed_service:
        depends = f"""    depends_on:
      {seed_service}:
        condition: service_healthy
"""
    return f"""  {name}:
    image: logos-sim:local
    hostname: {name}
    labels:
      harness.group: "{g['name']}"
    entrypoint: ["/opt/sim/entrypoint-member.sh"]
    environment:
{envblock}
{depends}    volumes:
      - ./out/traces:/traces
    networks:
      sim:
        ipv4_address: {ip}
"""


def generate(m, compose_path, targets_path, plan_path):
    net_conf = m["network"]
    net = ipaddress.ip_network(net_conf["subnet"])

    # A seed group holds exactly one seed (manifest.load enforces it), and the
    # seed's service is named after its group.
    seeds = {g["name"]: g for g in m["groups"] if g["kind"] == "seed"}
    taken = {ipaddress.ip_address(g["ip"]) for g in seeds.values()}
    dht_entry = manifest.seed_addr(next(iter(seeds.values()))) if seeds else ""

    services, targets, plan = [], [], []
    member_no = 0

    for g in m["groups"]:
        names = []
        if g["kind"] == "seed":
            name = g["name"]
            first = manifest.seed_addr(g) == dht_entry
            services.append(_seed_service(g, net_conf, name, "" if first else dht_entry))
            targets.append(
                {
                    "targets": [f"{g['ip']}:{SEED_METRICS_PORT}"],
                    "labels": {"node": name, "role": "seed", "group": g["name"]},
                }
            )
            names.append(name)
        else:
            ips = _addresses(net, g["count"], taken)
            for ip in ips:
                member_no += 1
                name = f"m{member_no}"
                # Round-robin over the whole fleet, so late joiners alternate too.
                seed = seeds[g["seeds"][(member_no - 1) % len(g["seeds"])]]
                services.append(
                    _member_service(
                        g, name, ip, manifest.seed_addr(seed), net_conf, seed["name"]
                    )
                )
                targets.append(
                    {
                        "targets": [f"{ip}:{METRICS_PORT}"],
                        "labels": {"node": name, "role": "member", "group": g["name"]},
                    }
                )
                names.append(name)
        step = {
            "name": g["name"],
            "kind": g["kind"],
            "startAfter": g["startAfter"],
            "services": names,
        }
        if "stopAfter" in g:
            step["stopAfter"] = g["stopAfter"]
        plan.append(step)

    with open(compose_path, "w") as f:
        f.write("# generated by harness gen -- do not edit\n")
        f.write("services:\n")
        f.write("\n".join(services))

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
        print(
            f"  +{g['startAfter']:>5}s  {g['name']:<16} {len(g['services'])} node(s){stop}"
        )
