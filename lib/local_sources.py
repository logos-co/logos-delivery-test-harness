"""Build the RLN stack from local checkouts instead of from GitHub.

HARNESS_LOCAL_SRC=<dir> names the folder that holds them, laid out as below.
The module and crate checkouts are mounted read-only into the nix builder at
/src/<folder> and used as `git+file:///src/<folder>?ref=<branch>`: the committed
state of that branch, never the working tree. logos-lez-rln is mounted into the
LEZ build container (docker/build-lez.sh). Unset, every source is the GitHub reference the
manifest and lib/manifest.py name.

The RLN module repositories pin a crates-only repository as a cargo git
dependency. The module builder fetches it with git while nix evaluates, so
a git `insteadOf` rule in the builder points its GitHub URL at the local
folder too; the pinned commits must exist there.
"""
import os

# The layout expected under HARNESS_LOCAL_SRC: each folder is a git checkout.
# Component -> folder.
FLAKES = {
    "logos-delivery-module": "logos-delivery-module",
    "rln_lez_module": "rln-0.4/logos-rln-lez-module",
    "membership_allocation_client_module": "rln-0.4/logos-membership-allocation-client-module",
    "rln_module": "rln-0.4/logos-rln-module",
}
# GitHub URL -> folder, for the cargo git dependencies.
CRATES = {
    "https://github.com/logos-co/logos-membership-allocation-crates": "rln-0.4/logos-membership-allocation-crates",
}
# logos-lez-rln, for the LEZ tools and guest programs (docker/build-lez.sh).
LEZ_RLN = "rln-0.4/logos-lez-rln"
MOUNT = "/src"


def root():
    """The folder HARNESS_LOCAL_SRC names, absolute; None when unset."""
    r = os.environ.get("HARNESS_LOCAL_SRC")
    return os.path.abspath(os.path.expanduser(r)) if r else None


def flake(comp):
    """The in-container flake URL of a component built from a local folder."""
    return f"git+file://{MOUNT}/{FLAKES[comp]}"


def lez_rln_dir():
    """logos-lez-rln on the host, or None."""
    return os.path.join(root(), LEZ_RLN) if root() else None


def _folders():
    return [*FLAKES.values(), *CRATES.values()]


def check():
    """Every folder is there and is a git repository; the list of problems."""
    r = root()
    return [
        f"{os.path.join(r, f)} is not a git checkout"
        for f in [*_folders(), LEZ_RLN]
        if not os.path.isdir(os.path.join(r, f, ".git"))
    ]


def mounts():
    """`docker run` arguments, one per line when printed: each folder alone,
    so nothing else under HARNESS_LOCAL_SRC is visible to the builder."""
    r = root()
    args = []
    for f in _folders():
        args += ["-v", f"{os.path.join(r, f)}:{MOUNT}/{f}:ro"]
    return args


def prelude():
    """Shell lines for the builder container: git may read the mounted folders
    (they belong to another user) and finds the crate repository in them."""
    if not root():
        return ""
    lines = ["cat > /etc/gitconfig <<'GITCONFIG'", "[safe]", "\tdirectory = *"]
    for url, folder in CRATES.items():
        lines += [f'[url "file://{MOUNT}/{folder}"]', f"\tinsteadOf = {url}"]
    lines += ["GITCONFIG", "cp /etc/gitconfig \"$HOME/.gitconfig\" 2>/dev/null || true"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import sys

    what = sys.argv[1]
    if what == "mounts":
        if root():
            problems = check()
            if problems:
                sys.exit("harness: HARNESS_LOCAL_SRC: " + "; ".join(problems))
            print("\n".join(mounts()))
    elif what == "prelude":
        print(prelude(), end="")
    elif what == "lez-rln":
        print(lez_rln_dir() or "")
