"""Record where the demo lives in the nix store: out/demo-paths.json.

The builder container has no python, sed or grep, so it only drops raw files
in out/: the `run-logos-standalone-ui` wrapper nix generated, the store path of
the dev libp2p_module, and `nix flake metadata` of the demo. This reads them on
the host. The wrapper is the one place the store paths are written down:

  exec <standalone-app>/bin/logos-standalone-app --modules-dir "<modules>" "<plugin>" "$@"
"""
import json
import os
import re
import sys


def extract(out):
    with open(os.path.join(out, "demo-wrapper.sh")) as f:
        wrapper = f.read()
    m = re.search(
        r'exec (\S+/bin/logos-standalone-app) --modules-dir "([^"]+)" "([^"]+)"', wrapper
    )
    if not m:
        sys.exit("harness: demo wrapper not in the expected shape: " + wrapper[-300:])
    with open(os.path.join(out, "demo-libp2p-path")) as f:
        libp2p = f.read().strip()
    with open(os.path.join(out, "demo-meta.json")) as f:
        meta = json.load(f)
    return {
        "app": m.group(1),
        "modules": m.group(2),
        "plugin": m.group(3),
        "libp2pModule": os.path.join(libp2p, "modules", "libp2p_module"),
        "rev": meta.get("revision", ""),
        "url": meta.get("url", ""),
    }


if __name__ == "__main__":
    out = sys.argv[1]
    paths = extract(out)
    with open(os.path.join(out, "demo-paths.json"), "w") as f:
        json.dump(paths, f, indent=2)
        f.write("\n")
    print(f"demo {paths['rev'][:12]}: {paths['plugin']}")
