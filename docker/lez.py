#!/usr/bin/env python3
"""The LEZ side of an RLN run, around logos-lez-rln's tools.

  lez.py init    before the sequencer: mint the payer that pays the deployment's
                 fees and write the sequencer config that funds it at genesis
  lez.py setup   once the sequencer answers: deploy an RLN registry, publish its
                 id, then fund every account a node asks for

State lives in /lez (out/lez on the host). The wallet that holds the payer's
key (home/) stays with these two services; the sequencer mounts seq/ only, and
the nodes pub/ only:
  seq/sequencer_config.json  the sequencer's config            (init)
  pub/registry-id            logos:local:<hex>, what a node's RLN preset names (setup)
  pub/fund/<node>            written by a node: the LEZ account to fund, then
                             optionally the amount
  pub/funded/<node>          written here: "ok", or the error

The recipe is logos-delivery-module's tests/e2e (conftest.py lez_registry).
"""
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request

LEZ = "/lez"
HOME = f"{LEZ}/home"
PUB = f"{LEZ}/pub"
TOOLS = "/opt/lez-rln"
SEQUENCER = os.environ.get("LEZ_RLN_SEQUENCER", "http://lez-sequencer:3040/")
# LEZ's CLOCK_50 account and its clock shard, with the timestamp in ms at bytes
# 8-16: 0 until block 50, and the registry refuses registrations until then.
CLOCK_50 = "4BdcjoXkq786TMWcBGGHqcxeLYMZmn17rL4eM9ZyRWkX"
CLOCK_SHARD = "2hwaRW8sMnyKLLRbDRw8k68tiv9bnfq98oxcdtcp157Z"
GENESIS_BALANCE = 10**13
FUNDING = int(os.environ.get("LEZ_FUNDING", 10**10))
# The chain line (manifest rln.lezVersion): "0.3" is logos-execution-zone
# v0.3.2, "0.4" is v0.4.0-rc1 and "0.4-rc3" v0.4.0-rc3, each with
# logos-lez-rln's tools for it.
V04 = os.environ.get("LEZ_VERSION", "0.3").startswith("0.4")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def log(*a):
    print("[harness lez]", *a, flush=True)


def tool(name, *args, env=None):
    r = subprocess.run(
        [f"{TOOLS}/target/release/{name}", *args], cwd=TOOLS,
        capture_output=True, text=True,
        # LEE_WALLET_PASSWORD: a throwaway wallet, unencrypted, so no tool prompts.
        env={**os.environ, "HOME": HOME, "LEE_WALLET_HOME_DIR": HOME,
             "LEE_WALLET_PASSWORD": "",
             # risc0's executor, where the tools image carries one (build-lez.sh).
             "PATH": f"{TOOLS}/bin:{os.environ.get('PATH', '')}",
             **(env or {})},
    )
    if r.returncode != 0:
        raise RuntimeError(f"{name} exited {r.returncode}: {(r.stdout + r.stderr)[-600:]}")
    return r.stdout


def rpc(method, *params):
    req = urllib.request.Request(
        SEQUENCER,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)["result"]


def wait(what, cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except (OSError, KeyError, ValueError):
            pass
        time.sleep(1)
    sys.exit(f"[harness lez] FAILED: {what} within {timeout}s")


def b58_hex(text):
    n = 0
    for c in text:
        n = n * 58 + B58.index(c)
    return n.to_bytes(32, "big").hex()


def b58(hex_id):
    raw = bytes.fromhex(hex_id)
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def fund(payer, account, amount=FUNDING):
    """Transfer `amount` to `account` (hex, as a node's wallet reports it) and see
    it arrive. fund_account returns once the transfer is submitted, and two
    submitted within one block share the payer's nonce: the second is dropped.

    On the v0.4 line `account` is the payer's public key: an account that was
    never written is credited only by a transfer that presents its key. There
    fund_account itself waits for the transfer's outcome and fails otherwise."""
    if V04:
        tool("fund_account", "--to-public-key", account, "--amount", str(amount),
             env={"LEZ_RLN_PAYER": payer})
        return 1
    for attempt in range(1, 6):
        tool("fund_account", "--to", account, "--amount", str(amount),
             env={"LEZ_RLN_PAYER": payer})
        for _ in range(20):
            if rpc("getAccountBalance", b58(account)) > 0:
                return attempt
            time.sleep(0.5)
    raise RuntimeError(f"no balance at {account} after 5 transfers")


def config_v04(payer_key):
    """lez/sequencer/service/networks/local.json of v0.4.0-rc1, with the payer's
    public key as the only genesis account (genesis credits a key, not an
    account id) and 500 ms blocks."""
    return {
        "home": ".",
        "max_num_tx_in_block": 20,
        "mempool_max_size": 10000,
        "block_create_timeout": "500ms",
        "retry_pending_blocks_timeout": "5s",
        "bedrock_config": {"channel_id": "01" * 32, "node_url": "http://localhost:18080"},
        "genesis": {
            "channel_params": {
                "minimum_sequencer_stake": 149,
                "posting_timeframe": 300,
                "posting_timeout": 25,
                "exit_delay": 100,
                "max_block_size": 1048576,
            },
            "actions": [{"supply_account": {
                "recipient": {"identified": {"key": payer_key}},
                "balance": GENESIS_BALANCE,
            }}],
        },
        "gossip": {"listen_addr": "/ip4/0.0.0.0/udp/0/quic-v1", "bootstrap_peers": []},
    }


def init():
    os.makedirs(HOME, exist_ok=True)
    os.makedirs(f"{LEZ}/seq", exist_ok=True)
    # v0.3 prints the payer's account id; v0.4 the id, then its public key.
    minted = tool("mint_payer").split()
    payer = minted[0] if V04 else minted[-1]
    with open(f"{LEZ}/payer", "w") as f:
        f.write(payer)
    # lez/sequencer/service/configs/docker/sequencer_config.json, with the payer
    # as the only genesis account and 500 ms blocks, so CLOCK_50 ticks in ~25 s.
    config = config_v04(minted[1]) if V04 else {
        "home": "/var/lib/sequencer_service",
        "max_num_tx_in_block": 20,
        "mempool_max_size": 10000,
        "block_create_timeout": "500ms",
        "retry_pending_blocks_timeout": "7s",
        "bedrock_config": {
            "channel_id": "01" * 32,
            "node_url": "http://host.docker.internal:18080",
            "funding_key": "2e03b2eff5a45478e7e79668d2a146cf2c5c7925bce927f2b1c67f2ab4fc0d26",
            "channel_params": {
                "minimum_sequencer_stake": 149,
                "posting_timeframe": 300,
                "posting_timeout": 25,
                "exit_delay": 100,
            },
        },
        "genesis": [{"supply_account": {"account_id": payer, "balance": GENESIS_BALANCE}}],
    }
    with open(f"{LEZ}/seq/sequencer_config.json", "w") as f:
        json.dump(config, f)
    with open(f"{HOME}/wallet_config.json", "w") as f:
        json.dump({
            "sequencer": {"sequencer_addr": SEQUENCER},
            "seq_poll_timeout": "500ms",
            "seq_tx_poll_max_blocks": 60,
            "seq_poll_max_retries": 10,
            "seq_block_poll_max_amount": 100,
        }, f)
    # The sequencer runs as uid 1000 and only reads its config.
    os.chmod(f"{LEZ}/seq", 0o755)
    os.chmod(f"{LEZ}/seq/sequencer_config.json", 0o644)
    os.chmod(HOME, 0o700)
    log("payer", payer, "minted; sequencer config written")


def clock_ms():
    shard = rpc("getAccount", CLOCK_50)["data"]["shards"].get(CLOCK_SHARD, [])
    return int.from_bytes(bytes(shard[8:16]), "little")


def deploy(payer):
    """Deploy one RLN registry with a tree of its own; returns its id."""
    out = tool("run_setup", env={
        "LEZ_RLN_PAYER": payer,
        "LEZ_RLN_TREE_ID_HEX": secrets.token_hex(32),
        "RISC0_DEV_MODE": "1",
    })
    account = next((l.split()[-1] for l in out.splitlines() if l.startswith("Config account:")), None)
    if not account:
        sys.exit(f"[harness lez] FAILED: run_setup printed no config account:\n{out[-600:]}")
    return f"logos:local:{b58_hex(account)}"


def publish(name, registry):
    with open(f"{PUB}/{name}.tmp", "w") as f:
        f.write(registry)
    os.rename(f"{PUB}/{name}.tmp", f"{PUB}/{name}")


def setup():
    with open(f"{LEZ}/payer") as f:
        payer = f.read().strip()
    wait("the sequencer produced no block", lambda: rpc("getLastBlockId") >= 1, 120)
    registry = deploy(payer)
    # LEZ_MIX_REGISTRY=1: a second registry, for Mix RLN (manifest rln.mixRegistry).
    mix_registry = deploy(payer) if os.environ.get("LEZ_MIX_REGISTRY") == "1" else None
    # The registry refuses registrations until CLOCK_50 ticked, at block 50.
    # v0.4 serves accounts in another shape, so there the block height decides.
    if V04:
        wait("the chain never reached block 60", lambda: rpc("getLastBlockId") >= 60, 180)
    else:
        wait("CLOCK_50 never ticked", lambda: clock_ms() > 0, 180)
    os.makedirs(f"{PUB}/fund", exist_ok=True)
    os.makedirs(f"{PUB}/funded", exist_ok=True)
    if mix_registry:
        publish("mix-registry-id", mix_registry)
        log("mix registry", mix_registry, "deployed")
    # Published last: a node that reads it can ask for funding at once.
    publish("registry-id", registry)
    log("registry", registry, "deployed; CLOCK_50 live; funding on request")
    while True:
        for node in sorted(os.listdir(f"{PUB}/fund")):
            done = f"{PUB}/funded/{node}"
            if node.endswith(".tmp") or os.path.exists(done):
                continue
            with open(f"{PUB}/fund/{node}") as f:
                request = f.read().split()
            if not request:
                continue  # the node is still writing it
            # "<account>", or "<account> <amount>" (an rln-allocator, which
            # pays for every node's registrations).
            account = request[0]
            try:
                amount = int(request[1]) if len(request) > 1 else FUNDING
                attempts = fund(payer, account, amount)
                result = "ok"
                log("funded", node, account, amount, f"(transfer {attempts})" if attempts > 1 else "")
            except (RuntimeError, OSError, KeyError, ValueError) as e:
                result = str(e)
                log("FAILED funding", node, account, result)
            with open(done, "w") as f:
                f.write(result)
        time.sleep(1)


if __name__ == "__main__":
    {"init": init, "setup": setup}[sys.argv[1]]()
