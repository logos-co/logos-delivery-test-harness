"""Turn the module's flake lock into a concrete build list.

`harness.sh build` runs `nix flake metadata --json` on the chosen module inside
the builder container and drops the result at out/lock.json. This turns that
into out/resolved.json: one flake reference per component.

By default every component except the module itself comes from that lock, so a
single module pin decides the whole set. An override -- from the manifest or
from the environment (see manifest.ENV_PREFIX) -- replaces one component's
source, its revision, or both.

Two wrinkles the lock imposes:
  * openmetrics-module and logos-logoscore-cli are TRANSITIVE nodes, not root
    inputs, so the whole node map has to be searched, not just module inputs.
  * the map carries suffixed duplicates (foo_2, foo_100, ...), so an exact name
    match wins over any suffixed one.
"""
import json
import sys

import manifest


def _base_url(locked):
    """The flake URL of a lock entry, without any revision on it."""
    kind = locked.get("type")
    if kind == "github":
        return f"github:{locked['owner']}/{locked['repo']}"
    if kind == "git":
        url = locked.get("url", "")
        # The lock stores a bare transport URL; nix needs the flake scheme.
        return url if url.startswith("git+") else "git+" + url
    return None


def _compose(base, rev=None, ref=None, submodules=False):
    parts = []
    if rev:
        parts.append("rev=" + rev)
    elif ref:
        parts.append("ref=" + ref)
    if submodules:
        parts.append("submodules=1")
    if not parts:
        return base
    sep = "&" if "?" in base else "?"
    return base + sep + "&".join(parts)


def _already_pinned(flake):
    """A flake string the caller pinned themselves, e.g. `github:o/r/<sha>`."""
    if "rev=" in flake or "ref=" in flake:
        return True
    tail = flake.rsplit("/", 1)[-1]
    return flake.startswith("github:") and manifest._is_sha(tail)


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
    module_pinned = (
        _compose(module_ref, rev=module_rev)
        if module_rev and "rev=" not in module_ref
        else module_ref
    )

    for comp, output in manifest.OUTPUTS.items():
        if comp == manifest.MODULE:
            resolved[comp] = {
                "flake": module_pinned,
                "output": output,
                "source": "manifest",
            }
            continue

        ov = overrides.get(comp) or {}
        node = _find(nodes, comp)
        locked = dict((node or {}).get("locked") or {})

        if "flake" in ov:
            # A source of its own: the module's lock has no say over it.
            base, submodules = ov["flake"], False
            if not (ov.get("rev") or ov.get("ref") or _already_pinned(base)):
                missing.append(f"{comp} (override names a flake but no rev/ref)")
                continue
            rev, ref = ov.get("rev"), ov.get("ref")
        else:
            base = _base_url(locked) if locked else None
            if base is None:
                missing.append(comp)
                continue
            submodules = bool(locked.get("submodules"))
            # A ref replaces the locked revision; a rev replaces it in kind.
            ref = ov.get("ref")
            rev = None if ref else (ov.get("rev") or locked.get("rev"))
            if not (rev or ref):
                missing.append(comp)
                continue

        resolved[comp] = {
            "flake": _compose(base, rev, ref, submodules),
            "output": output,
            "source": "override" if ov else "module-lock",
        }

    if manifest.uses_demo(m):
        # Not an input of the module: its own source, main unless overridden.
        # build records the commit a branch resolved to (out/demo-paths.json).
        ov = overrides.get(manifest.DEMO) or {}
        base = ov.get("flake", manifest.DEMO_FLAKE)
        rev, ref = ov.get("rev"), ov.get("ref")
        if not (rev or ref or _already_pinned(base)):
            ref = manifest.DEMO_DEFAULT_REF
        resolved[manifest.DEMO] = {
            "flake": _compose(base, rev, ref),
            "output": manifest.DEMO_OUTPUT,
            "source": "override" if ov else "default",
        }

    if missing:
        print(
            "harness: the chosen module ref does not pin: " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            'harness: give each one a full reference under overrides, e.g.\n'
            '  "libp2p_module": {"flake": "git+https://github.com/logos-co/'
            'logos-libp2p-module", "rev": "<sha>"}\n'
            "harness: or set LIBP2P_MODULE_REF / _REV / _FLAKE in the environment",
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
