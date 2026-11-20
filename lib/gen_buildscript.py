"""Emit the shell script that runs inside the nixos/nix builder container.

The container holds nix and nothing else -- no python, no jq -- so the manifest
is resolved on the host and this script is generated with the flake references
already substituted. Everything the container does is `nix build`.
"""
import json
import sys

import local_sources
import manifest

NIX_CONF = """mkdir -p /etc/nix
{
  echo "experimental-features = nix-command flakes"
  echo "sandbox = false"
  echo "filter-syscalls = false"
  echo "extra-substituters = https://cache.nix.logos.co/public https://cache.nix.logos.co/ci"
  echo "extra-trusted-public-keys = public:l4HrXgL4nw246+LBh2SOJyhz64BoGegOYLheT/iIAPU= ci:aVJqjS4NWX5WfqHO0AEhIScjGu/JyK5FoydPrnEbMvc="
  echo "fallback = true"
  # A cache that does not answer costs this long per lookup before nix builds
  # from source instead.
  echo "connect-timeout = 5"
} > /etc/nix/nix.conf
"""

# nix's git cache is kept in the store volume, so it survives between runs.
# HTTP/1.1: a large pack over HTTP/2 from inside Docker fails mid-transfer.
FETCH_SETUP = """export XDG_CACHE_HOME=/nix/.cache
git config --system http.version HTTP/1.1
"""

# Components whose `modules/` directory is copied into the image's module dir.
# The RLN ones are resolved only for a manifest with an "rln" section.
MODULE_COMPONENTS = [
    "libp2p_module", "openmetrics-module",
    "rln_lez_module", "membership_allocation_client_module", "rln_module",
    "logos-delivery-module",
]


# The demo runs straight from the nix volume rather than from a copy in an image
# (its closure is ~3.4 GB), so what it needs is rooted there and survives a GC.
GCROOTS = "/nix/var/nix/gcroots/harness"


def demo_builds(comps, lines):
    """logos-delivery-demo, built against the fleet's own delivery_module.

    The demo pins a delivery_module release in its flake. Both overrides are
    needed: the first swaps the module's source, but nix keeps the demo lock's
    entry for the module's own `logos-delivery` input unless it is overridden
    too -- a demo on the fleet's module, compiled against another delivery.
    The standalone host loads only `-dev` module variants, so libp2p_module is
    built again as `#install` (dev) at the fleet's rev; the portable build the
    members use is refused.
    """
    demo, mod = comps[manifest.DEMO], comps[manifest.MODULE]
    dv, lp = comps["logos-delivery"], comps["libp2p_module"]
    ref = f"{demo['flake']}#{demo['output']}"
    lines += [
        f"mkdir -p {GCROOTS}",
        f'echo "--- {manifest.DEMO}  {ref}"',
        f"nix build -L --no-write-lock-file "
        f'--override-input delivery_module "{mod["flake"]}" '
        f'--override-input delivery_module/logos-delivery "{dv["flake"]}" '
        f'"{ref}" -o {GCROOTS}/demo',
        f'echo "--- libp2p_module (dev variant, for the demo host)  {lp["flake"]}#install"',
        f'nix build -L --no-write-lock-file "{lp["flake"]}#install" -o {GCROOTS}/libp2p-dev',
        f'nix flake metadata --json --no-write-lock-file "{demo["flake"]}" > /out/demo-meta.json',
        # A copy out of the store is read-only; the next build must replace it.
        "rm -f /out/demo-wrapper.sh",
        f"cp {GCROOTS}/demo/bin/run-logos-standalone-ui /out/demo-wrapper.sh",
        f"readlink -f {GCROOTS}/libp2p-dev > /out/demo-libp2p-path",
    ]


def generate(resolved, out_path):
    comps = resolved["components"]
    lines = ["set -euo pipefail", NIX_CONF, local_sources.prelude(), FETCH_SETUP]

    def build(key, link, extra=""):
        c = comps[key]
        ref = f"{c['flake']}#{c['output']}"
        lines.append(f'echo "--- {key}  {ref}"')
        # --no-write-lock-file: a flake whose own lock is not fully pinned makes
        # nix want to update it in place, which it cannot do for a remote flake
        # and which we would not want anyway -- the rev we asked for is the rev
        # we build. master of logos-delivery-module is one such flake.
        lines.append(f'nix build -L --no-write-lock-file {extra}"{ref}" -o {link}')

    def delivery_override():
        """Carry a chosen logos-delivery into the module's own build.

        logosdeliverynode is built straight from logos-delivery, but delivery_module --
        what delivery-module nodes run -- is compiled inside the module's build against
        the module's `logos-delivery` input. Two separate nix invocations, so
        without this a chosen delivery would move the native nodes and leave the
        module nodes on whatever the module pins: a fleet built from two different
        deliveries, silently. Nothing is passed when delivery comes from the
        module's lock, which is the default -- then they already agree.
        """
        dv = comps["logos-delivery"]
        if dv["source"] == "module-lock":
            return ""
        return f'--override-input logos-delivery "{dv["flake"]}" '

    build("logos-logoscore-cli", "/tmp/r-core")
    def rln_overrides():
        """Build rln_module against the RLN modules built here.

        Its flake names rln_lez_module and the allocation client by branch, so
        a standalone build would take whatever the branches point at today and
        compile a second LEZ wallet for it.
        """
        return "".join(
            f'--override-input {name} "{comps[name]["flake"]}" '
            for name in manifest.RLN_MODULE_INPUTS["rln_module"]
        )

    modules = [k for k in MODULE_COMPONENTS if k in comps]
    for i, key in enumerate(modules):
        extra = ""
        if key == "logos-delivery-module":
            extra = delivery_override()
            if "rln_module" in comps:
                # The delivery module names the three RLN modules as inputs.
                flakes = {k: c["flake"] for k, c in comps.items()}
                extra += "".join(f'{a} ' if a.startswith("--") or a in manifest.RLN_SOURCES else f'"{a}" '
                                 for a in manifest.rln_input_overrides(flakes))
        if key == "rln_module":
            extra = rln_overrides()
        build(key, f"/tmp/r-{i}", extra)
    build("logos-delivery", "/tmp/r-seed")
    if manifest.DEMO in comps:
        demo_builds(comps, lines)

    # Collected in the container first and made writable there: store paths are
    # read-only, and a read-only directory created straight in /out cannot be
    # filled when /out is a bind mount written as the host's user (Docker Desktop).
    stage = "/tmp/stage"
    lines += [
        'echo "--- collecting into /out"',
        "chmod -R u+w /out/logoscore /out/modules /out/seed-store 2>/dev/null || true",
        "rm -rf /out/logoscore /out/modules /out/seed-store",
        f"rm -rf {stage}; mkdir -p {stage}",
        f"cp -rL /tmp/r-core {stage}/logoscore",
        f"mkdir -p {stage}/modules",
        f"cp -rL /tmp/r-core/modules/. {stage}/modules/",
    ]
    for i, _ in enumerate(modules):
        lines.append(f"cp -rL /tmp/r-{i}/modules/. {stage}/modules/")
    lines += [
        f"mkdir -p {stage}/seed-store",
        f'for p in $(nix path-info -r /tmp/r-seed); do cp -a "$p" {stage}/seed-store/; done',
        f"chmod -R u+w {stage}",
        f"cp -a {stage}/logoscore {stage}/modules {stage}/seed-store /out/",
        f"rm -rf {stage}",
        "readlink -f /tmp/r-seed > /out/seed-path",
        'echo "modules:"; ls /out/modules',
        'echo "seed: $(cat /out/seed-path)  closure: $(du -sh /out/seed-store | cut -f1)"',
    ]

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        generate(json.load(f), sys.argv[2])
