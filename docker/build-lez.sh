#!/bin/bash
# Runs inside the docker/Dockerfile.lez-build container, in two steps:
#   build-lez.sh guest   logos-lez-rln's guest programs, with cargo-risczero.
#                        linux/amd64 only: risc0 ships its toolchain for that
#                        platform, and the programs build in docker.
#   build-lez.sh tools   the three tools the LEZ services run, for this
#                        container's platform, into /out/lez-tools, laid out as
#                        they expect (run_setup reads its guest programs
#                        relative to lez-rln/).
# The tools are built natively because an amd64 run_setup dies of SIGILL under
# Rosetta. /work is a volume shared by both steps; cargo's home and target dirs
# are per platform. The toolchain pins are those of logos-delivery-module's
# .github/actions/setup-lez-rln; the commit is harness.sh's LEZ_RLN_REV.
# LEZ_RLN_LOCAL=<dir>: the commit is exported from that checkout, not cloned.
set -euo pipefail
LEZ_RLN_REV=${LEZ_RLN_REV:?the logos-lez-rln commit to build}
RZUP_VERSION=${RZUP_VERSION:-0.5.2}
CARGO_RISCZERO_VERSION=${CARGO_RISCZERO_VERSION:-3.0.5}
RISC0_RUST_VERSION=${RISC0_RUST_VERSION:-1.88.0}

arch=$(uname -m)
export CARGO_HOME=/work/cargo-$arch RISC0_HOME=/work/risc0
export PATH=$CARGO_HOME/bin:/work/risc0/bin:$PATH
mkdir -p "$CARGO_HOME" "$RISC0_HOME"

git config --global --add safe.directory '*'
if [ -n "${LEZ_RLN_LOCAL:-}" ]; then
  src=/work/logos-lez-rln-$LEZ_RLN_REV
  if [ ! -f "$src/.exported" ]; then
    rm -rf "$src"; mkdir -p "$src"
    git -C "$LEZ_RLN_LOCAL" archive "$LEZ_RLN_REV" | tar -x -C "$src"
    touch "$src/.exported"
  fi
  cd "$src"
else
  src=/work/logos-lez-rln
  [ -d "$src/.git" ] || git clone https://github.com/logos-co/logos-lez-rln.git "$src"
  cd "$src"
  git fetch -q origin
  git checkout -q "$LEZ_RLN_REV"
fi
guest=methods/guest/target/riscv32im-risc0-zkvm-elf/docker

case "${1:-}" in
  guest)
    command -v rzup >/dev/null || cargo install rzup --version "$RZUP_VERSION" --locked
    rzup install cargo-risczero "$CARGO_RISCZERO_VERSION"
    rzup install rust "$RISC0_RUST_VERSION"
    cargo risczero build --manifest-path lez-rln/methods/guest/Cargo.toml
    ls -l lez-rln/$guest
    ;;
  tools)
    # methods/build.rs would build the guest programs again, with the risc0
    # toolchain, and then strips the user ELF in the docker-built ones: skip the
    # first, and give the second any llvm-strip.
    export RISC0_SKIP_BUILD=1 RISC0_LLVM_STRIP=${RISC0_LLVM_STRIP:-$(command -v llvm-strip)}
    export CARGO_TARGET_DIR=/work/target-$arch
    # nssa/prove: the wallet runs each program deployment through LEZ's executor
    # before sending it. In-process with `prove`, as in the sequencer; without
    # it that executor is r0vm, which risc0 ships for linux/amd64 only.
    cargo build --release --manifest-path lez-rln/Cargo.toml --features nssa/prove \
      --bin mint_payer --bin run_setup --bin fund_account
    dst=/out/lez-tools/lez-rln
    rm -rf /out/lez-tools
    mkdir -p "$dst/target/release" "$dst/$guest"
    cp "$CARGO_TARGET_DIR"/release/{mint_payer,run_setup,fund_account} "$dst/target/release/"
    cp lez-rln/$guest/*.bin "$dst/$guest/"
    cp -r lez-rln/resources "$dst/"
    echo "$LEZ_RLN_REV" > /out/lez-tools/rev
    ls -l "$dst/target/release" "$dst/$guest"
    ;;
  *) echo "usage: build-lez.sh guest|tools" >&2; exit 1 ;;
esac
