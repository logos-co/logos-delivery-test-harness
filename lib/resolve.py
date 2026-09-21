"""Turn the module's flake lock into a concrete build list.

`harness build` runs `nix flake metadata --json` on the chosen module inside the
builder container and drops the result at out/lock.json. This turns that into
out/resolved.json: one flake reference per component, every one pinned to a rev.

Two wrinkles the lock imposes:
  * openmetrics-module and logos-logoscore-cli are TRANSITIVE nodes, not root
    inputs, so the whole node map has to be searched, not just module inputs.
  * the map carries suffixed duplicates (foo_2, foo_100, ...), so an exact name
    match wins over any suffixed one.
"""
import json
import sys

import manifest


def _flake_ref(locked):
    """Reconstruct a pinned flake reference from a lock entry."""
    kind = locked.get("type")
    rev = locked.get("rev")
    if not rev:
        return None
    if kind == "github":
        return f"github:{locked['owner']}/{locked['repo']}/{rev}"
    if kind == "git":
        url = locked["url"]
        # The lock stores a bare transport URL; nix needs the flake scheme.
        if not url.startswith("git+"):
            url = "git+" + url
        sep = "&" if "?" in url else "?"
        ref = f"{url}{sep}rev={rev}"
        if locked.get("submodules"):
            ref += "&submodules=1"
        return ref
    return None


def _pin(flake, rev):
    """Attach a rev to a flake reference the caller gave without one."""
    if not rev or f"rev={rev}" in flake or flake.endswith(rev):
        return flake
    return f"{flake}{'&' if '?' in flake else '?'}rev={rev}"


def _find(nodes, name):
    if name in nodes:
        return nodes[name]
    # Fall back to the lowest-numbered suffixed duplicate, which nix emits for
    # repeated inputs of the same flake.
    dupes = sorted(k for k in nodes if k.startswith(name + "_"))
    return nodes[dupes[0]] if dupes else None


def resolve(m, lock_path, out_path):
    with open(lock_path) as f:
        lock = json.load(f)
    nodes = lock.get("locks", lock).get("nodes", {})

    overrides = m["overrides"]
    resolved = {}
    missing = []

    # `nix flake metadata` reports the rev the module ref actually resolved to.
    # Record that rather than the branch name, so resolved.json reproduces.
    module_ref = manifest.module_flake(m)
    module_rev = (lock.get("locked") or {}).get("rev")
    module_pinned = _pin(module_ref, module_rev) if module_rev else module_ref

    for comp, output in manifest.OUTPUTS.items():
        if comp == "logos-delivery-module":
            # The module is the pin, not a lock entry.
            resolved[comp] = {
                "flake": module_pinned,
                "output": output,
                "source": "manifest",
            }
            continue

        ov = overrides.get(comp)
        # A full reference in an override stands on its own: the chosen module
        # ref need not pin the component at all.
        if ov and "flake" in ov:
            resolved[comp] = {
                "flake": _pin(ov["flake"], ov.get("rev")),
                "output": output,
                "source": "override",
            }
            continue

        node = _find(nodes, comp)
        if node is None:
            missing.append(comp)
            continue
        locked = dict(node.get("locked") or {})
        source = "module-lock"
        if ov and "rev" in ov:
            locked["rev"] = ov["rev"]
            source = "override"

        ref = _flake_ref(locked)
        if ref is None:
            missing.append(comp)
            continue
        resolved[comp] = {"flake": ref, "output": output, "source": source}

    if missing:
        print(
            "harness: the chosen module ref does not pin: " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            'harness: give each one a full reference under overrides, e.g.\n'
            '  "libp2p_module": {"flake": "git+https://github.com/logos-co/'
            'logos-libp2p-module", "rev": "<sha>"}',
            file=sys.stderr,
        )
        sys.exit(1)

    doc = {"module": module_pinned, "components": resolved}
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return doc


if __name__ == "__main__":
    m = manifest.load(sys.argv[1])
    doc = resolve(m, sys.argv[2], sys.argv[3])
    print(f"module   {doc['module']}")
    for name, c in doc["components"].items():
        print(f"  {name:<24} {c['source']:<12} {c['flake']}#{c['output']}")
