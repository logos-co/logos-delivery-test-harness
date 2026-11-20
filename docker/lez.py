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
  pub/fund/<node>            written by a node: the LEZ account to fund and its public key
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
GENESIS_BALANCE = 10**13
FUNDING = int(os.environ.get("LEZ_FUNDING", 10**10))
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def log(*a):
    print("[harness lez]", *a, flush=True)


def tool(name, *args, env=None):
    r = subprocess.run(
        [f"{TOOLS}/target/release/{name}", *args], cwd=TOOLS,
        capture_output=True, text=True,
        # SUPPRESS_VERBOSE_PRINTS: the wallet otherwise prints every transaction
        # it sends, megabytes for a program deployment.
        env={**os.environ, "HOME": HOME, "LEE_WALLET_HOME_DIR": HOME, "LEE_WALLET_PASSWORD": "",
             "SUPPRESS_VERBOSE_PRINTS": "1", **(env or {})},
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


def fund(payer, account, public_key):
    """Transfer FUNDING to `account` (hex, as a node's wallet reports it) and see
    it arrive. The account has never been written, so the transfer names its
    public key: a credit by account id lands only on an account that exists.
    fund_account waits for the transfer's block and fails when it is not in one."""
    error = None
    for attempt in range(1, 6):
        try:
            tool("fund_account", "--to-public-key", public_key, "--amount", str(FUNDING),
                 env={"LEZ_RLN_PAYER": payer})
        except RuntimeError as e:
            error = e
        for _ in range(20):
            if rpc("getAccountBalance", b58(account)) > 0:
                return attempt
            time.sleep(0.5)
    raise RuntimeError(f"no balance at {account} after 5 transfers" + (f": {error}" if error else ""))


def init():
    os.makedirs(HOME, exist_ok=True)
    os.makedirs(f"{LEZ}/seq", exist_ok=True)
    # mint_payer prints the account id, then its public key: genesis credits a key.
    payer, public_key = tool("mint_payer").split()[:2]
    with open(f"{LEZ}/payer", "w") as f:
        f.write(payer)
    # lez/sequencer/service/networks/local.json, with the payer as the only
    # genesis account and 500 ms blocks. The sequencer image sets a home up from
    # this file; the Bedrock funding key is its --funding-public-key.
    config = {
        "home": ".",
        "max_num_tx_in_block": 20,
        "mempool_max_size": 10000,
        "block_create_timeout": "500ms",
        "retry_pending_blocks_timeout": "7s",
        "bedrock_config": {
            "channel_id": "01" * 32,
            "node_url": "http://host.docker.internal:18080",
        },
        "genesis": {
            "channel_params": {
                "minimum_sequencer_stake": 149,
                "posting_timeframe": 300,
                "posting_timeout": 25,
                "exit_delay": 100,
                "max_block_size": 1048576,
            },
            "actions": [{"supply_account": {
                "recipient": {"identified": {"key": public_key}},
                "balance": GENESIS_BALANCE,
            }}],
        },
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


def setup():
    with open(f"{LEZ}/payer") as f:
        payer = f.read().strip()
    wait("the sequencer produced no block", lambda: rpc("getLastBlockId") >= 1, 120)
    out = tool("run_setup", env={
        "LEZ_RLN_PAYER": payer,
        "LEZ_RLN_TREE_ID_HEX": secrets.token_hex(32),
        "RISC0_DEV_MODE": "1",
    })
    account = next((l.split()[-1] for l in out.splitlines() if l.startswith("Config account:")), None)
    if not account:
        sys.exit(f"[harness lez] FAILED: run_setup printed no config account:\n{out[-600:]}")
    registry = f"logos:local:{b58_hex(account)}"
    os.makedirs(f"{PUB}/fund", exist_ok=True)
    os.makedirs(f"{PUB}/funded", exist_ok=True)
    # Published last: a node that reads it can ask for funding at once.
    with open(f"{PUB}/registry-id.tmp", "w") as f:
        f.write(registry)
    os.rename(f"{PUB}/registry-id.tmp", f"{PUB}/registry-id")
    log("registry", registry, "deployed; funding on request")
    while True:
        for node in sorted(os.listdir(f"{PUB}/fund")):
            done = f"{PUB}/funded/{node}"
            if node.endswith(".tmp") or os.path.exists(done):
                continue
            with open(f"{PUB}/fund/{node}") as f:
                request = f.read().split()
            if not request:
                continue  # the node is still writing it
            account = request[0]
            try:
                if len(request) < 2:
                    raise ValueError("the request names no public key")
                attempts = fund(payer, account, request[1])
                result = "ok"
                log("funded", node, account, f"(transfer {attempts})" if attempts > 1 else "")
            except (RuntimeError, OSError, KeyError, ValueError) as e:
                result = str(e)
                log("FAILED funding", node, account, result)
            with open(done, "w") as f:
                f.write(result)
        time.sleep(1)


if __name__ == "__main__":
    {"init": init, "setup": setup}[sys.argv[1]]()
