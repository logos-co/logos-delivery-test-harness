"""Load and validate harness.json.

The manifest pins exactly one thing -- the logos-delivery-module ref -- and
declares the node groups. Everything else is derived (see resolve.py).
"""
import json
import os
import sys

# Component -> the flake output to build. Keys are node names in the module's
# flake lock, except "logos-delivery-module" which is the module itself.
OUTPUTS = {
    "logos-logoscore-cli": "cli-bundle-dir",
    "libp2p_module": "install-portable",
    "openmetrics-module": "install-portable",
    "logos-delivery-module": "install-portable",
    "logos-delivery": "logosdeliverynode",
}

MODULE = "logos-delivery-module"
    ## The one component that is a pin rather than a lock entry.

# Per-component environment prefix: <PREFIX>_FLAKE / _REF / _REV. Setting any
# of these overrides what the module's lock would otherwise supply, which is
# the default for everything except the module itself.
ENV_PREFIX = {
    "logos-delivery-module": "DELIVERY_MODULE",
    "logos-delivery": "DELIVERY",
    "libp2p_module": "LIBP2P_MODULE",
    "openmetrics-module": "OPENMETRICS_MODULE",
    "logos-logoscore-cli": "LOGOSCORE_CLI",
}

INFRA_HOSTS = 256  # first addresses of the subnet: seeds, prometheus, grafana
# Fixed in docker-compose.yml; a seed placed here would collide.
RESERVED_IPS = {"10.0.0.11": "prometheus", "10.0.0.12": "grafana"}

# secp256k1, for deriving a seed's peer id from its nodekey.
_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fail(msg):
    print(f"harness: {msg}", file=sys.stderr)
    sys.exit(1)


def _is_sha(s):
    """A full git object name. Short shas are not accepted by nix as a rev."""
    return len(s) == 40 and all(c in "0123456789abcdefABCDEF" for c in s)


def _env_pin(prefix):
    """`{flake,ref,rev}` from <PREFIX>_FLAKE / _REF / _REV; empty if none set.

    `_REF` takes a branch, a tag, or a full sha -- a sha is moved to `rev`,
    because nix will not accept one as a `ref`. `_REV` says so explicitly.
    """
    flake = os.environ.get(prefix + "_FLAKE")
    ref = os.environ.get(prefix + "_REF")
    rev = os.environ.get(prefix + "_REV")
    if ref and not rev and _is_sha(ref):
        ref, rev = None, ref

    pin = {}
    if flake:
        pin["flake"] = flake
    if rev:
        pin["rev"] = rev
    elif ref:
        pin["ref"] = ref
    return pin


def _apply_pin(target, pin):
    """Merge a pin in, where naming a rev clears a ref and vice versa."""
    if not pin:
        return
    if "flake" in pin:
        target["flake"] = pin["flake"]
    if "rev" in pin:
        target["rev"] = pin["rev"]
        target.pop("ref", None)
    elif "ref" in pin:
        target["ref"] = pin["ref"]
        target.pop("rev", None)


def _point_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        slope = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P)
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    slope %= _P
    x = (slope * slope - a[0] - b[0]) % _P
    return (x, (slope * (a[0] - x) - a[1]) % _P)


def peer_id(nodekey):
    """The libp2p peer id of a secp256k1 private key given as 64 hex digits.

    Identity multihash over the protobuf PublicKey (type 2 = secp256k1, the
    33-byte compressed point), base58btc -- what logosdeliverynode --nodekey
    turns into. Lets a seed be declared by its key alone.
    """
    if len(nodekey) != 64:
        raise ValueError("nodekey must be 64 hex digits")
    k = int(nodekey, 16)
    if not 0 < k < _N:
        raise ValueError("nodekey out of range")
    point, acc = _G, None
    while k:
        if k & 1:
            acc = _point_add(acc, point)
        point = _point_add(point, point)
        k >>= 1
    x, y = acc
    pub = bytes([2 + (y & 1)]) + x.to_bytes(32, "big")
    proto = bytes([0x08, 0x02, 0x12, len(pub)]) + pub
    raw = bytes([0x00, len(proto)]) + proto
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def load(path):
    if not os.path.exists(path):
        _fail(f"no manifest at {path}")
    try:
        with open(path) as f:
            m = json.load(f)
    except ValueError as e:
        _fail(f"{path} is not valid JSON: {e}")

    mod = m.get("module") or {}
    if not mod.get("flake"):
        _fail("manifest.module.flake is required")

    _apply_pin(mod, _env_pin(ENV_PREFIX[MODULE]))

    if not (mod.get("ref") or mod.get("rev")):
        mod["ref"] = "master"

    net = m.setdefault("network", {})
    net.setdefault("subnet", "10.0.0.0/8")
    net.setdefault("clusterId", 42)
    net.setdefault("shardsInNetwork", 1)

    groups = m.get("groups") or []
    if not groups:
        _fail("manifest.groups is empty")

    names = set()
    seed_ips = {}
    for g in groups:
        for key in ("name", "kind", "count"):
            if key not in g:
                _fail(f"group is missing '{key}': {g}")
        if g["kind"] not in ("seed", "member"):
            _fail(f"group '{g['name']}': kind must be seed or member")
        if g["name"] in names:
            _fail(f"duplicate group name '{g['name']}'")
        names.add(g["name"])
        g.setdefault("startAfter", 0)
        g.setdefault("jitter", 0)
        g.setdefault("env", {})
        # stopAfter: seconds from T0 at which the group's containers are
        # stopped (SIGTERM, then compose's 10 s grace). Absent: they run on.
        if "stopAfter" in g and g["stopAfter"] <= g["startAfter"]:
            _fail(f"group '{g['name']}': stopAfter must come after startAfter")
        if g["kind"] == "member":
            g.setdefault("config", "member.json.tpl")
        else:
            _check_seed(g, seed_ips)

    seeds = [g["name"] for g in groups if g["kind"] == "seed"]
    for g in groups:
        if g["kind"] != "member":
            continue
        # The seeds this group's members bootstrap from, handed out round-robin.
        # A member takes a single bootstrap peer: the plugin passes libp2p only
        # the first (see run-node.md, "Plugin-hosted discovery").
        g.setdefault("seeds", seeds)
        for s in g["seeds"]:
            if s not in seeds:
                _fail(f"group '{g['name']}': no seed group named '{s}'")
            seed = next(x for x in groups if x["name"] == s)
            # compose would otherwise start the seed early, as a dependency.
            if seed["startAfter"] > g["startAfter"]:
                _fail(f"group '{g['name']}' starts before its seed '{s}'")
        if not g["seeds"]:
            _fail(f"group '{g['name']}': members need a seed group to bootstrap from")

    # Overrides carry a _comment key for humans; drop it.
    overrides = {
        k: v for k, v in (m.get("overrides") or {}).items() if not k.startswith("_")
    }
    # Every other component takes its pin from the module's lock unless the
    # environment says otherwise, in which case that wins over the manifest too.
    for comp, prefix in ENV_PREFIX.items():
        if comp == MODULE:
            continue
        pin = _env_pin(prefix)
        if not pin:
            continue
        merged = dict(overrides.get(comp) or {})
        _apply_pin(merged, pin)
        overrides[comp] = merged
    m["overrides"] = overrides
    return m


def _check_seed(g, seed_ips):
    """One seed per group: it has a fixed address and identity."""
    if g["count"] != 1:
        _fail(
            f"seed group '{g['name']}': count must be 1 -- a seed has a fixed "
            "ip and nodekey, so declare each seed as its own group"
        )
    for key in ("ip", "port", "nodekey"):
        if key not in g:
            _fail(f"seed group '{g['name']}' is missing '{key}'")
    if g["ip"] in RESERVED_IPS:
        _fail(f"seed group '{g['name']}': {g['ip']} is {RESERVED_IPS[g['ip']]}'s address")
    if g["ip"] in seed_ips:
        _fail(f"seed groups '{seed_ips[g['ip']]}' and '{g['name']}' share {g['ip']}")
    seed_ips[g["ip"]] = g["name"]
    try:
        derived = peer_id(g["nodekey"])
    except ValueError:
        _fail(f"seed group '{g['name']}': nodekey must be 64 hex digits of a valid key")
    if g.setdefault("peerId", derived) != derived:
        _fail(
            f"seed group '{g['name']}': peerId {g['peerId']} does not belong to its "
            f"nodekey, which gives {derived} -- drop peerId to have it derived"
        )
    g.setdefault("args", [])


def module_flake(m):
    """The flake reference for the module itself, with its chosen ref or rev."""
    mod = m["module"]
    sep = "&" if "?" in mod["flake"] else "?"
    if mod.get("rev"):
        return f"{mod['flake']}{sep}rev={mod['rev']}"
    return f"{mod['flake']}{sep}ref={mod['ref']}"


def seed_addr(g):
    """The /p2p/ multiaddr of a seed group."""
    return f"/ip4/{g['ip']}/tcp/{g['port']}/p2p/{g['peerId']}"
