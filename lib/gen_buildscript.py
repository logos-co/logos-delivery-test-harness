"""Emit the shell script that runs inside the nixos/nix builder container.

The container holds nix and nothing else -- no python, no jq -- so the manifest
is resolved on the host and this script is generated with the flake references
already substituted. Everything the container does is `nix build`.
"""
import json
import sys

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

# Components whose `modules/` directory is copied into the image's module dir.
# The RLN ones (resolved["rln"], for a manifest with an "rln" section) are
# built between these and the delivery module.
MODULE_COMPONENTS = ["libp2p_module", "openmetrics-module"]


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
    lines = ["set -euo pipefail", NIX_CONF]

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
    rln = resolved.get("rln") or {}

    def rln_override():
        """Build the RLN module against the LEZ provider and the allocator
        client built here.

        Its flake may name those inputs by branch, so a standalone build would
        take whatever the branch points at today and compile a second LEZ
        wallet for it. The delivery module's flake pins the inputs the same way.
        """
        return "".join(
            f'--override-input {rln[part]} "{comps[rln[part]]["flake"]}" '
            for part in ("lez", "allocator") if part in rln
        )

    def allocator_override():
        """The allocator client generates its libp2p_module client at build
        time: from the fleet's libp2p_module, where it has that input."""
        if rln["allocator"] == "rln_gifter_module":
            return ""
        return libp2p_override()

    def libp2p_override():
        return f'--override-input libp2p_module "{comps["libp2p_module"]["flake"]}" '

    extras = {manifest.MODULE: delivery_override, manifest.ALLOCATOR_MODULE: libp2p_override}
    modules = [k for k in MODULE_COMPONENTS if k in comps]
    for part, extra in (("lez", None), ("allocator", allocator_override), ("module", rln_override)):
        if part in rln:
            modules.append(rln[part])
            if extra:
                extras[rln[part]] = extra
    if manifest.ALLOCATOR_MODULE in comps:
        modules.append(manifest.ALLOCATOR_MODULE)
    modules.append(manifest.MODULE)
    for i, key in enumerate(modules):
        build(key, f"/tmp/r-{i}", extras[key]() if key in extras else "")
    build("logos-delivery", "/tmp/r-seed")
    if manifest.DEMO in comps:
        demo_builds(comps, lines)

    lines += [
        'echo "--- collecting into /out"',
        "chmod -R u+w /out/logoscore /out/modules /out/seed-store 2>/dev/null || true",
        "rm -rf /out/logoscore /out/modules /out/seed-store",
        "cp -rL /tmp/r-core /out/logoscore",
        "mkdir -p /out/modules",
        "cp -rL /tmp/r-core/modules/. /out/modules/",
    ]
    for i, _ in enumerate(modules):
        lines.append(f"cp -rL /tmp/r-{i}/modules/. /out/modules/")
    lines += [
        "mkdir -p /out/seed-store",
        'for p in $(nix path-info -r /tmp/r-seed); do cp -a "$p" /out/seed-store/; done',
        "readlink -f /tmp/r-seed > /out/seed-path",
        "chmod -R u+w /out/logoscore /out/modules /out/seed-store",
        'echo "modules:"; ls /out/modules',
        'echo "seed: $(cat /out/seed-path)  closure: $(du -sh /out/seed-store | cut -f1)"',
    ]

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        generate(json.load(f), sys.argv[2])
