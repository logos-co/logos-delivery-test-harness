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

INFRA_HOSTS = 256  # first addresses of the subnet: seed, prometheus, grafana


def _fail(msg):
    print(f"harness: {msg}", file=sys.stderr)
    sys.exit(1)


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
        if g["kind"] == "member":
            g.setdefault("config", "member.json.tpl")
        else:
            for key in ("ip", "port", "peerId", "nodekey"):
                if key not in g:
                    _fail(f"seed group '{g['name']}' is missing '{key}'")
            g.setdefault("args", [])

    # Overrides carry a _comment key for humans; drop it.
    m["overrides"] = {
        k: v for k, v in (m.get("overrides") or {}).items() if not k.startswith("_")
    }
    return m


def module_flake(m):
    """The flake reference for the module itself, with its chosen ref or rev."""
    mod = m["module"]
    sep = "&" if "?" in mod["flake"] else "?"
    if mod.get("rev"):
        return f"{mod['flake']}{sep}rev={mod['rev']}"
    return f"{mod['flake']}{sep}ref={mod['ref']}"


def seed_group(m):
    for g in m["groups"]:
        if g["kind"] == "seed":
            return g
    return None


def seed_addr(m):
    g = seed_group(m)
    if g is None:
        return ""
    return f"/ip4/{g['ip']}/tcp/{g['port']}/p2p/{g['peerId']}"
