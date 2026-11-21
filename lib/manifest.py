"""Load and validate harness.json.

The manifest pins exactly one thing -- the logos-delivery-module ref -- and
declares the node groups. Everything else is derived (see resolve.py).
"""
import json
import os
import re
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

# Built on top of those for a manifest with an "rln" section: the RLN module
# delivery_module calls, and the LEZ registry provider that one calls. Both are
# nodes of the module's lock, like libp2p_module.
#
# Their names depend on the module ref. Each row is (RLN module, LEZ registry
# provider, allocator client); the first row whose RLN module the lock names is
# the one built. The allocator client is built only where the lock names it.
RLN_MODULE_SETS = [
    # The split repositories (delivery_module 0.4.0 and later).
    ("rln_module", "rln_lez_module", "membership_allocation_client_module"),
    # logos-rln-modules, one repository with a subdirectory per module.
    ("liblogos_rln_module", "liblogos_lez_rln_module", "rln_gifter_module"),
]
RLN_OUTPUT = "install-portable"

# rln.lezVersion -> what `harness build` builds the local chain from: the
# logos-execution-zone tag of the sequencer, the logos-lez-rln revision of the
# registry tools (it must target that tag), and the suffix of their artefacts.
LEZ_VERSIONS = {
    "0.3": {"ref": "v0.3.2", "rlnRev": "235ddaeee84eec385a2d4b68d69f702a29c14cf3", "suffix": ""},
    "0.4": {"ref": "v0.4.0-rc1", "rlnRev": "a06b04455d20e129ee974f211cf3a3c6fcdb21b3", "suffix": "0.4"},
    # Same recipe as "0.4" (docker/lez.py, Dockerfile.lez-sequencer-0.4). Its
    # wallet simulates a transaction before sending it, in risc0's r0vm: the
    # tools image carries one, at the risc0 version the wallet resolves.
    "0.4-rc3": {"ref": "v0.4.0-rc3", "rlnRev": "cf5e1f578a05fe5a66d845597c83c62b224aa19c",
                "suffix": "0.4-rc3", "dockerfile": "0.4", "r0vm": "3.0.5"},
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
    "logos-delivery-demo": "DELIVERY_DEMO",
}

INFRA_HOSTS = 256  # first addresses of the subnet: bootstraps, prometheus, grafana
# Fixed in docker-compose.yml; a bootstrap placed here would collide.
RESERVED_IPS = {
    "10.0.0.11": "prometheus", "10.0.0.12": "grafana",
    "10.0.0.13": "lez-sequencer", "10.0.0.14": "lez-init", "10.0.0.15": "lez-setup",
}
LEZ_SEQUENCER_IP = "10.0.0.13"
LEZ_SEQUENCER_PORT = 3040

# secp256k1, for deriving a bootstrap's peer id from its nodekey.
_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

DEFAULT_IMAGE = "logos-sim:local"
# What a sending group's messages should come to (lib/msg_report.py):
# delivered to every receiver, a messageError for every one, or not judged.
EXPECT = ("delivered", "error", "any")

ROLES = ("bootstrap", "member")
NATIVE = "logosdeliverynode"  # the delivery node binary, in-process kademlia
MODULE_NODE = "delivery-module"  # logoscore + delivery_module + libp2p_module
DEMO_NODE = "delivery-demo"  # the logos-delivery-demo UI, viewable over noVNC
AGENT_NODE = "sd-agent"  # logoscore + libp2p_module only, driven by docker/sd_agent.py
# logoscore + libp2p_module + the LEZ registry provider + the RLN membership
# allocator (docker/entrypoint-allocator.sh): registers memberships for the
# RLN nodes from its own payer. Needs rln.allocator in the manifest.
ALLOCATOR_NODE = "rln-allocator"
KINDS = (NATIVE, MODULE_NODE, DEMO_NODE, AGENT_NODE, ALLOCATOR_NODE)
# The allocator's module. Not an input of the delivery module: rln.allocator
# pins it ({"rev": <sha>}, optionally "flake").
ALLOCATOR_MODULE = "membership_allocation_module"
ALLOCATOR_FLAKE = "github:logos-co/logos-membership-allocation-module"
DEFAULT_KIND = {"bootstrap": NATIVE, "member": MODULE_NODE}

# Ports inside every container. A native node listens on its tcp port and is
# its own DHT peer; a module node's DHT runs on libp2p_module's port, under the
# libp2p key, so that is the address a peer bootstraps from.
NATIVE_BOOTSTRAP_PORT = 44001
NATIVE_METRICS_PORT = 8008
MODULE_TCP_PORT = 44000
MODULE_P2P_PORT = 45000
MODULE_METRICS_PORT = 9100
DEMO_VNC_PORT = 6080  # noVNC inside a demo container; published as 6080 + n

# The demo is not an input of the module, so it has a source of its own. It is
# built only for a manifest that runs one, against the fleet's delivery_module.
DEMO = "logos-delivery-demo"
DEMO_FLAKE = "github:logos-co/logos-delivery-demo"
DEMO_DEFAULT_REF = "main"
DEMO_OUTPUT = "ui-dev"

# Flags the harness sets on every native node itself; a group's args would
# only fight them.
HARNESS_OWNED_FLAGS = (
    "--cluster-id", "--shard", "--nat", "--tcp-port", "--nodekey",
    "--metrics-server", "--kad-bootstrap-node",
)


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
    turns into, and what libp2p_module makes of the same key as its privKey.
    Lets a bootstrap be declared by its key alone.
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

    # The image `build` tags and the nodes run: one per build variant, so a
    # manifest that pins another logos-delivery does not replace the default.
    if not isinstance(m.setdefault("image", DEFAULT_IMAGE), str) or not m["image"]:
        _fail("manifest.image must be an image name, e.g. \"logos-sim:pr4647\"")
    # How long `run` and `suite` let the stack run, in seconds from T0.
    if "duration" in m and not (isinstance(m["duration"], int) and m["duration"] > 0):
        _fail("manifest.duration must be a number of seconds")

    net = m.setdefault("network", {})
    net.setdefault("subnet", "10.0.0.0/8")
    net.setdefault("clusterId", 42)
    net.setdefault("shardsInNetwork", 1)
    # spread: members get addresses that share as few leading bits with each
    # other and the bootstraps as the subnet allows (spread_addresses), instead
    # of the hash scatter.
    net.setdefault("spread", False)

    # "rln": {} (or with overrides) turns the local LEZ chain and its RLN
    # registry on; a group then opts in with env RLN=1.
    if m.get("rln") is not None:
        rln = m["rln"] = {k: v for k, v in m["rln"].items() if not k.startswith("_")}
        rln.setdefault("epochSizeSec", 10)
        rln.setdefault("maxEpochGap", 3)
        # lezVersion: the chain line the RLN modules of the chosen module ref
        # read. "0.3" is logos-execution-zone v0.3.2; "0.4" is v0.4.0-rc1, with
        # another account format, genesis and funding (docker/lez.py);
        # "0.4-rc3" is v0.4.0-rc3.
        rln.setdefault("lezVersion", "0.3")
        if rln["lezVersion"] not in LEZ_VERSIONS:
            _fail(f"rln.lezVersion must be one of {sorted(LEZ_VERSIONS)}")
        suffix = LEZ_VERSIONS[rln["lezVersion"]]["suffix"]
        rln.setdefault("sequencerImage", "logos-sim-lez-sequencer:" + (suffix or "local"))
        rln.setdefault("toolsImage", "logos-sim-lez:" + (suffix or "local"))
        # allocator: memberships come from an rln-allocator node, not from each
        # node's own funded payer.
        if rln.get("allocator") is not None:
            alloc = rln["allocator"]
            alloc.setdefault("flake", ALLOCATOR_FLAKE)
            if not _is_sha(alloc.get("rev") or ""):
                _fail("rln.allocator needs \"rev\": the full commit of the allocator module")

    groups = m.get("groups") or []
    if not groups:
        _fail("manifest.groups is empty")

    names = set()
    bootstrap_ips = {}
    for g in groups:
        _check_group(g, names)
        # A native node with --preset takes its cluster from the preset alone.
        if not _native_preset(g):
            g.setdefault("clusterId", net["clusterId"])
        if g["role"] == "bootstrap":
            _check_bootstrap(g, bootstrap_ips)

    bootstraps = [g["name"] for g in groups if g["role"] == "bootstrap"]
    # Bootstraps 2..N join the first one's DHT.
    for b in bootstraps[1:]:
        first, g = _group(groups, bootstraps[0]), _group(groups, b)
        if _network(g) != _network(first):
            _fail(
                f"bootstrap group '{b}' is on {_network(g)}, the first bootstrap "
                f"'{first['name']}' on {_network(first)}"
            )
    for g in groups:
        if g["role"] != "member":
            continue
        # The bootstraps this group's members join through: one each,
        # round-robin, or with `joinAll` every one of them, in this order.
        # `extraBootstraps` are multiaddrs put in front of those, e.g. a peer
        # that is never up, to see a member get past it.
        # An explicit [] joins an external DHT instead: the group's config
        # (e.g. a network preset) names the kad bootstraps, the harness none.
        if "bootstraps" not in g and not bootstraps:
            _fail(
                f"group '{g['name']}': manifest has no bootstrap group; give members "
                "one to join through, or \"bootstraps\": [] to join an external DHT "
                "named by their config"
            )
        g.setdefault("bootstraps", bootstraps)
        g.setdefault("joinAll", False)
        if not isinstance(g["joinAll"], bool):
            _fail(f"group '{g['name']}': joinAll must be true or false")
        g.setdefault("extraBootstraps", [])
        for addr in g["extraBootstraps"]:
            if not (isinstance(addr, str) and addr.startswith("/") and "/p2p/" in addr
                    and "," not in addr):
                _fail(f"group '{g['name']}': extraBootstraps entry is not a /p2p/ multiaddr: {addr!r}")
        for b in g["bootstraps"]:
            if b not in bootstraps:
                _fail(f"group '{g['name']}': no bootstrap group named '{b}'")
            boot = _group(groups, b)
            # compose would otherwise start the bootstrap early, as a dependency.
            if boot["startAfter"] > g["startAfter"]:
                _fail(f"group '{g['name']}' starts before its bootstrap '{b}'")
            if _network(boot) != _network(g):
                _fail(
                    f"group '{g['name']}' is on {_network(g)}, its bootstrap '{b}' "
                    f"on {_network(boot)}"
                )

    for g in groups:
        if str(g["env"].get("RLN", "0")) == "1":
            if not m.get("rln"):
                _fail(f"group '{g['name']}': env RLN=1 needs an \"rln\" section in the manifest")
            if g["kind"] != MODULE_NODE:
                _fail(f"group '{g['name']}': RLN runs on {MODULE_NODE} nodes only")
        if g["kind"] == ALLOCATOR_NODE and not uses_allocator(m):
            _fail(f"group '{g['name']}': an {ALLOCATOR_NODE} node needs rln.allocator in the manifest")
    if uses_allocator(m) and not any(g["kind"] == ALLOCATOR_NODE for g in groups):
        _fail(f"rln.allocator needs a group of kind {ALLOCATOR_NODE}")

    # Overrides carry a _comment key for humans; drop it.
    overrides = {
        k: v for k, v in (m.get("overrides") or {}).items() if not k.startswith("_")
    }
    # As with <PREFIX>_REF: nix takes a full sha as a rev, not as a ref.
    for ov in overrides.values():
        if _is_sha(ov.get("ref") or "") and not ov.get("rev"):
            ov["rev"] = ov.pop("ref")
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


def _check_group(g, names):
    """Fields every group has; defaults filled in."""
    for key in ("name", "role", "count"):
        if key not in g:
            if key == "role" and g.get("kind") in ("seed", "member"):
                # The old shape: kind meant what role means now.
                new = "bootstrap" if g["kind"] == "seed" else "member"
                _fail(
                    f"group '{g.get('name')}': \"kind\": \"{g['kind']}\" is now "
                    f"\"role\": \"{new}\"; kind now picks the node "
                    f"({' or '.join(KINDS)})"
                )
            _fail(f"group is missing '{key}': {g}")
    if g["role"] not in ROLES:
        _fail(f"group '{g['name']}': role must be one of {', '.join(ROLES)}")
    g.setdefault("kind", DEFAULT_KIND[g["role"]])
    if g["kind"] not in KINDS:
        _fail(f"group '{g['name']}': kind must be one of {', '.join(KINDS)}")
    if g["name"] in names:
        _fail(f"duplicate group name '{g['name']}'")
    names.add(g["name"])
    g.setdefault("startAfter", 0)
    g.setdefault("jitter", 0)
    g.setdefault("env", {})
    if g.setdefault("expect", "delivered") not in EXPECT:
        _fail(f"group '{g['name']}': expect must be one of {', '.join(EXPECT)}")
    # stopAfter: seconds from T0 at which the group's containers are stopped.
    # Absent: they run on.
    if "stopAfter" in g and g["stopAfter"] <= g["startAfter"]:
        _fail(f"group '{g['name']}': stopAfter must come after startAfter")
    # manual: gen writes the group's services and build builds what they need,
    # but up/run never launch them -- `harness.sh start <group|node>` does, in
    # the running stack, and `stop` takes them down again.
    g.setdefault("manual", False)
    if g["manual"]:
        if g["role"] == "bootstrap":
            _fail(f"group '{g['name']}': a bootstrap cannot be manual -- members join through it from T0")
        if g["startAfter"] or "stopAfter" in g:
            _fail(
                f"group '{g['name']}': a manual group is started and stopped by hand "
                "(harness.sh start / stop); drop startAfter and stopAfter"
            )
    if g["kind"] == DEMO_NODE and g["role"] != "member":
        _fail(f"group '{g['name']}': a {DEMO_NODE} node can only be a member")
    if g["kind"] == NATIVE:
        g.setdefault("args", [])
        for a in g["args"]:
            flag = a.split("=")[0]
            # Joining an external DHT, the group names its own kad bootstraps.
            if flag == "--kad-bootstrap-node" and g.get("bootstraps") == []:
                continue
            if flag.startswith(HARNESS_OWNED_FLAGS):
                _fail(
                    f"group '{g['name']}': {a.split('=')[0]} is set by the harness "
                    "(identity, network, metrics, bootstrap); drop it from args"
                )
        # The harness passes no --cluster-id next to --preset: an explicit
        # cluster id overrides the preset's, and --cluster-id=2 even replaces
        # the preset with logos.dev (logos-delivery toNetworkPresetConf).
        if _native_preset(g) and "clusterId" in g:
            _fail(
                f"group '{g['name']}': --preset names the cluster; drop clusterId "
                "(a cluster id next to a preset overrides it)"
            )
    else:
        g.setdefault("config", "member.json.tpl")


def _check_bootstrap(g, bootstrap_ips):
    """One bootstrap per group: it has a fixed address and identity."""
    if g["count"] != 1:
        _fail(
            f"bootstrap group '{g['name']}': count must be 1 -- a bootstrap has a "
            "fixed ip and nodekey, so declare each as its own group"
        )
    for key in ("ip", "nodekey"):
        if key not in g:
            _fail(f"bootstrap group '{g['name']}' is missing '{key}'")
    if g["ip"] in RESERVED_IPS:
        _fail(f"bootstrap group '{g['name']}': {g['ip']} is {RESERVED_IPS[g['ip']]}'s address")
    if g["ip"] in bootstrap_ips:
        _fail(f"bootstrap groups '{bootstrap_ips[g['ip']]}' and '{g['name']}' share {g['ip']}")
    bootstrap_ips[g["ip"]] = g["name"]
    try:
        derived = peer_id(g["nodekey"])
    except ValueError:
        _fail(f"bootstrap group '{g['name']}': nodekey must be 64 hex digits of a valid key")
    if g.setdefault("peerId", derived) != derived:
        _fail(
            f"bootstrap group '{g['name']}': peerId {g['peerId']} does not belong to "
            f"its nodekey, which gives {derived} -- drop peerId to have it derived"
        )
    if g["kind"] == NATIVE:
        g.setdefault("port", NATIVE_BOOTSTRAP_PORT)
    elif "port" in g:
        _fail(
            f"bootstrap group '{g['name']}': a {MODULE_NODE} bootstrap is reached on "
            f"libp2p_module's port {MODULE_P2P_PORT}; drop 'port'"
        )


def module_flake(m):
    """The flake reference for the module itself, with its chosen ref or rev."""
    mod = m["module"]
    sep = "&" if "?" in mod["flake"] else "?"
    if mod.get("rev"):
        return f"{mod['flake']}{sep}rev={mod['rev']}"
    return f"{mod['flake']}{sep}ref={mod['ref']}"


def _group(groups, name):
    return next(g for g in groups if g["name"] == name)


def _native_preset(g):
    """The --preset a logosdeliverynode group's args name, else None."""
    if g["kind"] != NATIVE:
        return None
    args = g.get("args", [])
    for i, a in enumerate(args):
        if a.startswith("--preset="):
            return a.split("=", 1)[1]
        if a == "--preset" and i + 1 < len(args):
            return args[i + 1]
    return None


def _network(g):
    """What a node must share with the bootstrap it joins."""
    preset = _native_preset(g)
    return f"preset {preset}" if preset else f"cluster {g['clusterId']}"


def lez_sequencer_url():
    return f"http://{LEZ_SEQUENCER_IP}:{LEZ_SEQUENCER_PORT}/"


def uses_rln(m):
    """A manifest with an "rln" section runs a local LEZ chain with an RLN
    registry on it, and builds the RLN modules into the image."""
    return bool(m.get("rln"))


def uses_allocator(m):
    """Whether the RLN nodes ask an rln-allocator node for their memberships."""
    return bool((m.get("rln") or {}).get("allocator"))


def outputs(m):
    """Component -> flake output, for what every manifest builds. The RLN
    modules come on top, under the names the module's lock uses (resolve.py)."""
    return dict(OUTPUTS)


def image(m):
    """The runtime image of this manifest; IMAGE in the environment wins.
    The name goes into the generated compose file as it is, so it must be a
    plain image reference."""
    name = os.environ.get("IMAGE") or m["image"]
    if not re.fullmatch(r"[A-Za-z0-9._/-]+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?", name):
        _fail(f"image {name!r} is not an image reference (name[:tag][@sha256:digest])")
    return name


def uses_demo(m):
    return any(g["kind"] == DEMO_NODE for g in m["groups"])


def bootstrap_addr(g):
    """The /p2p/ multiaddr a peer bootstraps from: the node's own address for a
    native bootstrap, its libp2p_module's (same key, derived) for a module one."""
    port = g["port"] if g["kind"] == NATIVE else MODULE_P2P_PORT
    return f"/ip4/{g['ip']}/tcp/{port}/p2p/{g['peerId']}"


def x25519_public(priv_hex):
    """The Curve25519 public key of a 32-byte private key (RFC 7748), hex."""
    p, a24 = 2**255 - 19, 121665
    k = bytearray(bytes.fromhex(priv_hex))
    if len(k) != 32:
        raise ValueError("a mix key is 32 bytes")
    k[0] &= 248
    k[31] = (k[31] & 127) | 64
    k = int.from_bytes(k, "little")
    x1, x2, z2, x3, z3, swap = 9, 1, 0, 9, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b, c, d = (x2 + z2) % p, (x2 - z2) % p, (x3 + z3) % p, (x3 - z3) % p
        aa, bb, da, cb = a * a % p, b * b % p, d * a % p, c * b % p
        e = (aa - bb) % p
        x3, z3 = (da + cb) ** 2 % p, x1 * (da - cb) ** 2 % p
        x2, z2 = aa * bb % p, e * (aa + a24 * e) % p
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, "little").hex()


def mix_node(g):
    """The `--mixnode` entry (<multiaddr>:<mix public key>) of a bootstrap group
    with a fixed mix key, else None: a native one started with `--mixkey=`, or a
    module one with env NODEKEY and MIXKEY (the delivery node's own identity, on
    its tcp port -- not its libp2p_module's). Address, identity and mix key are
    all known ahead, so other nodes can be handed it as a mix hop before
    discovery finds any."""
    if g["role"] != "bootstrap":
        return None
    if g["kind"] == NATIVE:
        key = next((a.split("=", 1)[1] for a in g.get("args", []) if a.startswith("--mixkey=")), None)
        return f"{bootstrap_addr(g)}:{x25519_public(key)}" if key else None
    env = g.get("env", {})
    if g["kind"] != MODULE_NODE or not (env.get("NODEKEY") and env.get("MIXKEY")):
        return None
    return (f"/ip4/{g['ip']}/tcp/{MODULE_TCP_PORT}/p2p/{peer_id(env['NODEKEY'])}"
            f":{x25519_public(env['MIXKEY'])}")


def spread_addresses(net, count, taken):
    """`count` addresses in `net` that share as few leading bits as possible
    with each other and with `taken` (a set of ip_address, extended in place).

    The nim-libp2p registrar makes an advertiser wait advertExpiry * ipSim, and
    ipSim (iptree.ipScore) counts the tree levels on the advertiser's path whose
    counter exceeds total / 2^depth. Inside one /8 with fewer than 256 addresses
    in the tree, that is every level of the longest prefix shared with any held
    ad, so the wait follows the longest common prefix: pick each address to keep
    that as short as possible. Candidates: 2^7 prefixes of the leading free bits
    in bit-reversed order (coarse splits first), each with the host part 1.20."""
    free = 32 - net.prefixlen
    bits = min(7, free - 9)
    shift = free - bits
    cands = []
    for k in range(1 << bits):
        r = int(f"{k:0{bits}b}"[::-1], 2)
        ip = net[(r << shift) | 0x114]
        if (int(ip) - int(net.network_address) >= INFRA_HOSTS
                and str(ip) not in RESERVED_IPS):
            cands.append(ip)

    def shared(a):
        return max((32 - (int(a) ^ int(b)).bit_length() for b in taken), default=0)

    out = []
    for _ in range(count):
        free_cands = [c for c in cands if c not in taken]
        if not free_cands:
            _fail(f"network.spread: no address left in {net} for {count} nodes")
        best = min(free_cands, key=shared)
        taken.add(best)
        out.append(str(best))
    return out


def libp2p_privkey(g):
    """The nodekey as libp2p_module's privKey: a protobuf PrivateKey, type 2 =
    secp256k1, 32 bytes. Gives the module node the DHT identity peer_id() names."""
    return "08021220" + g["nodekey"].lower()
