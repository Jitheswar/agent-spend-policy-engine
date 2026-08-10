"""Generate/manage Algorand testnet accounts for the Agent Spend Policy Engine.

Usage:
    python scripts/setup_accounts.py generate      # create keys (idempotent)
    python scripts/setup_accounts.py balances       # show ALGO/USDC balances + opt-in status
    python scripts/setup_accounts.py optin          # opt every account into the network's USDC ASA
"""

import base64
import json
import os
import sys

import algosdk
from algosdk.transaction import AssetTransferTxn

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import algod, config  # noqa: E402

ACCOUNTS_PATH = config.ACCOUNTS_PATH
VERIFIED_MARKER_PATH = config.SETUP_VERIFIED_PATH
USDC_TESTNET_ASA_ID = config.USDC_ASA_ID

# What "funded enough to run the demo" means, and therefore what the
# .setup_verified marker is allowed to assert. The opt-in itself only needs
# ~0.2 ALGO; these are the thresholds for actually paying for API calls
# afterwards.
MIN_ALGO_MICRO = 100_000
MIN_USDC_MICRO = 1

# The treasury: receives every payment, and funds newly onboarded agents.
# Needs ALGO (it pays anchoring and provisioning fees) but never needs USDC.
TREASURY_ACCOUNT = config.ANCHOR_ACCOUNT


def account_names() -> list[str]:
    """Which accounts need to exist: the treasury, plus every agent policy
    knows about.

    Read from policy.json rather than kept as a list here. That list had
    already drifted -- accounts.json held two agents policy.json had never
    heard of -- and two sources of truth for "who are the agents" means one
    of them is always the wrong one to have edited. Agents onboarded at
    runtime land in policy.json too (see policy_store.add_agent), so they
    show up here automatically rather than being invisible to `balances`.
    """
    try:
        with open(config.POLICY_PATH) as f:
            agents = list(json.load(f).get("agents", {}))
    except (OSError, json.JSONDecodeError):
        agents = []
    return [TREASURY_ACCOUNT] + [a for a in agents if a != TREASURY_ACCOUNT]


def get_algod():
    return algod.client()


def _fetch_account_info(client, addr: str, attempts: int = 3, timeout: int = 12) -> dict:
    """account_info() with a couple of quick retries -- the public AlgoNode
    testnet endpoint is occasionally slow (multi-second responses aren't
    unusual, and it can occasionally exceed even a 30s single attempt), so
    a few shorter attempts are more resilient than one long one and fail
    faster when it's a real problem rather than transient slowness."""
    last_error = None
    for _ in range(attempts):
        try:
            return client.account_info(addr, timeout=timeout)
        except Exception as e:
            last_error = e
    raise last_error


def load_accounts() -> dict:
    if os.path.exists(ACCOUNTS_PATH):
        with open(ACCOUNTS_PATH) as f:
            return json.load(f)
    return {}


def save_accounts(accounts: dict) -> None:
    os.makedirs(os.path.dirname(ACCOUNTS_PATH), exist_ok=True)
    with open(ACCOUNTS_PATH, "w") as f:
        json.dump(accounts, f, indent=2)


def generate() -> None:
    accounts = load_accounts()
    changed = False
    # Existing entries are never removed, even if policy no longer lists
    # them: the account may hold funds, and losing its key to a config edit
    # would strand them.
    for name in account_names():
        if name in accounts:
            continue
        sk, addr = algosdk.account.generate_account()
        # algosdk's sk is already base64(32-byte seed || 32-byte pubkey) -- exactly
        # the AVM_PRIVATE_KEY format x402-avm expects.
        accounts[name] = {
            "address": addr,
            "mnemonic": algosdk.mnemonic.from_private_key(sk),
            "avm_private_key_b64": sk,
        }
        changed = True
    if changed:
        save_accounts(accounts)
    print(f"Accounts stored at {os.path.abspath(ACCOUNTS_PATH)}\n")
    for name, info in accounts.items():
        print(f"[{name}] {info['address']}")
    print("\nThese hold nothing yet, and payments fail until they do.")
    if config.FAUCETS:
        print(
            "Fund every address above. Both faucets need a human (sign-in +\n"
            "captcha, which is why this step can't be scripted):\n"
        )
        for label, url in config.FAUCETS:
            print(f"  {label:30s}: {url}")
    else:
        print(
            f"There is no faucet on {config.NETWORK} -- fund these from an exchange\n"
            "or a wallet you already control."
        )
    print(
        f"\nEvery address needs both assets, '{TREASURY_ACCOUNT}' included -- it pays\n"
        "anchoring fees in ALGO and funds newly onboarded agents out of its own\n"
        "USDC (see common/provisioning.py).\n"
        "\nThen: python3 scripts/setup_accounts.py optin"
        "\n      python3 scripts/setup_accounts.py balances"
        "\n\nFull walkthrough: GETTING_STARTED.md"
    )


def balances() -> None:
    accounts = load_accounts()
    if not accounts:
        print("No accounts yet. Run: python scripts/setup_accounts.py generate")
        return
    client = get_algod()
    for name, info in accounts.items():
        addr = info["address"]
        try:
            info_chain = _fetch_account_info(client, addr)
        except Exception as e:
            print(f"[{name}] {addr} -- couldn't reach the network to check ({e}). Try again in a moment.")
            continue
        algo_balance = info_chain.get("amount", 0) / 1_000_000
        assets = {a["asset-id"]: a["amount"] for a in info_chain.get("assets", [])}
        usdc = assets.get(USDC_TESTNET_ASA_ID)
        usdc_str = f"{usdc / 1_000_000:.2f} USDC" if usdc is not None else "NOT opted in"
        print(f"[{name:20s}] {addr}  |  {algo_balance:>8.3f} ALGO  |  {usdc_str}")


def _usdc_micro(acc_info: dict) -> int | None:
    """microUSDC held, or None if the account hasn't opted in."""
    for asset in acc_info.get("assets", []):
        if asset["asset-id"] == USDC_TESTNET_ASA_ID:
            return asset.get("amount", 0)
    return None


def optin() -> bool:
    """Returns True only if every account is opted in AND actually holds
    enough of both assets to run.

    That second half matters more than it looks. The return value is what
    decides whether start.sh writes data/.setup_verified, and once that
    marker exists start.sh stops checking balances on every launch. An
    earlier version returned True on opt-in success alone, so an account
    holding 0.2 ALGO and zero USDC got permanently marked "funded and opted
    in" -- and the first sign of trouble was a settlement failure in the
    middle of a demo, on a run that had deliberately skipped the check that
    would have caught it.
    """
    accounts = load_accounts()
    if not accounts:
        print("No accounts yet. Run: python scripts/setup_accounts.py generate")
        return False
    client = get_algod()
    params = client.suggested_params()
    all_ok = True
    for name, info in accounts.items():
        addr = info["address"]
        try:
            acc_info = _fetch_account_info(client, addr)
        except Exception as e:
            # This means "couldn't reach the network to check" (a timeout,
            # a connection error), NOT "this account doesn't exist" -- an
            # unfunded account still returns a normal zero-balance response
            # from algod, it doesn't error. Saying "fund it" here when the
            # real problem is a slow/unreachable node sends people to
            # re-fund an already-funded account for no reason.
            print(f"[{name}] skip -- couldn't reach the network to check ({e}). Try again in a moment.")
            all_ok = False
            continue
        algo_micro = acc_info.get("amount", 0)
        usdc_micro = _usdc_micro(acc_info)

        if usdc_micro is None:
            if algo_micro < 200_000:
                print(f"[{name}] skip -- balance too low to opt in, fund with ALGO first")
                all_ok = False
                continue
            sk = algosdk.mnemonic.to_private_key(info["mnemonic"])
            txn = AssetTransferTxn(
                sender=addr,
                sp=params,
                receiver=addr,
                amt=0,
                index=USDC_TESTNET_ASA_ID,
            )
            signed = txn.sign(sk)
            txid = client.send_transaction(signed)
            algosdk.transaction.wait_for_confirmation(client, txid, 4)
            print(f"[{name}] opted in to USDC-testnet, txid={txid}")
            # Freshly opted in means zero USDC by definition -- the ASA has
            # to be funded separately, so this run cannot claim the account
            # is ready no matter how well the opt-in went.
            usdc_micro = 0
        else:
            print(f"[{name}] already opted in to USDC-testnet")

        if algo_micro < MIN_ALGO_MICRO:
            print(f"[{name}] NOT ready -- {algo_micro / 1e6:.3f} ALGO, needs ALGO for fees")
            all_ok = False
        if usdc_micro < MIN_USDC_MICRO:
            print(f"[{name}] NOT ready -- 0 USDC, so it cannot pay for anything yet")
            all_ok = False

    if all_ok:
        with open(VERIFIED_MARKER_PATH, "w") as f:
            f.write("all accounts opted in and holding both assets -- delete to force a recheck\n")
    else:
        print(
            "\nNot writing data/.setup_verified: at least one account isn't ready.\n"
            "Fund the addresses above, then re-run ./start.sh -- it will check again."
        )
    return all_ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if cmd == "generate":
        generate()
    elif cmd == "balances":
        balances()
    elif cmd == "optin":
        sys.exit(0 if optin() else 1)
    else:
        print(__doc__)
        sys.exit(1)
