"""Onboard a brand-new agent at runtime: keypair, funding, USDC opt-in.

Registering an agent in policy.json is trivial -- it's a dict entry. The
part that isn't trivial, and the part that makes "add an agent live" a real
claim rather than a UI trick, is that a newly-created agent is useless
on-chain until three things are true: it has an Algorand address, it holds
ALGO to pay transaction fees, and it has opted in to the USDC ASA (Algorand
requires an explicit opt-in before an account can receive an asset -- an
un-opted-in account's payment is rejected, not queued).

So this module does the whole thing, from a public testnet with no faucet in
the loop: generate the key, fund the account from the server treasury (the
same account that receives x402 payments -- it's already funded, and we hold
its key), have the new agent opt itself in, then send it working capital.
About ten seconds of real on-chain activity, three confirmed transactions.

It runs in a background thread (see policy_engine/app.py) so the agent
appears in the dashboard instantly and its funding lands a few seconds
later, rather than the operator staring at a frozen button.

Re-onboarding an agent that already has an account reuses that account and
tops it up only if it's short -- so a demo can be re-run repeatedly without
generating a pile of dust accounts or spending down the treasury.
"""

import base64
import json
import os
import threading

import algosdk
from algosdk.transaction import AssetTransferTxn, PaymentTxn, wait_for_confirmation

from common import algod, config

ACCOUNTS_PATH = config.ACCOUNTS_PATH
USDC_ASA_ID = config.USDC_ASA_ID

# Enough ALGO to cover the 0.1 minimum balance, the extra 0.1 minimum an ASA
# opt-in imposes, and a long demo's worth of fees at 0.001 each.
FUND_ALGO = config.PROVISION_ALGO
FUND_USDC = config.PROVISION_USDC

# agent_id -> {"state": ..., "message": ..., "tx_ids": [...]}
# State machine: funding -> ready | failed. Read by GET /agents so the
# dashboard can show an agent as live-but-not-yet-spendable.
_status: dict[str, dict] = {}
_status_lock = threading.Lock()
_accounts_lock = threading.Lock()


def get_status(agent_id: str) -> dict | None:
    with _status_lock:
        status = _status.get(agent_id)
        return dict(status) if status else None


def all_statuses() -> dict:
    with _status_lock:
        return {k: dict(v) for k, v in _status.items()}


def _set_status(agent_id: str, state: str, message: str, tx_ids: list | None = None) -> None:
    with _status_lock:
        entry = _status.setdefault(agent_id, {"tx_ids": []})
        entry["state"] = state
        entry["message"] = message
        if tx_ids:
            # Deduped: the final "ready" update reports the whole run's
            # transactions, and steps along the way already reported their
            # own, so a plain append shows each one twice.
            seen = entry.get("tx_ids", [])
            entry["tx_ids"] = seen + [t for t in tx_ids if t not in seen]


def _load() -> dict:
    with open(ACCOUNTS_PATH) as f:
        return json.load(f)


def _save(accounts: dict) -> None:
    tmp = ACCOUNTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(accounts, f, indent=2)
    os.replace(tmp, ACCOUNTS_PATH)  # atomic: nothing ever reads a half-written key file


def ensure_account(agent_id: str) -> tuple[str, bool]:
    """Returns (address, created). Generates and persists a keypair if this
    agent doesn't have one yet; otherwise returns the existing address so
    re-onboarding is idempotent and doesn't strand the old account's funds.

    Locked because two concurrent onboards would otherwise read the same
    accounts.json and the second write would drop the first agent's key --
    losing key material for an account that may already hold testnet funds.
    """
    with _accounts_lock:
        accounts = _load()
        if agent_id in accounts:
            return accounts[agent_id]["address"], False
        sk, address = algosdk.account.generate_account()
        accounts[agent_id] = {
            "address": address,
            "mnemonic": algosdk.mnemonic.from_private_key(sk),
            # algosdk's sk is already base64(seed || pubkey) -- the exact
            # AVM_PRIVATE_KEY format x402-avm wants (see setup_accounts.py).
            "avm_private_key_b64": sk,
        }
        _save(accounts)
        return address, True


def _algod():
    return algod.client()


def _balances(client, address: str) -> tuple[int, int | None]:
    """(microALGO, microUSDC or None if not opted in)."""
    try:
        info = client.account_info(address)
    except Exception:
        return 0, None
    usdc = None
    for asset in info.get("assets", []):
        if asset.get("asset-id") == USDC_ASA_ID:
            usdc = asset.get("amount", 0)
    return info.get("amount", 0), usdc


def provision(agent_id: str, algo: float = FUND_ALGO, usdc: float = FUND_USDC) -> dict:
    """Fund a new agent from the server treasury and opt it into USDC.

    Every step is skipped if it's already true, so this is safe to re-run and
    fast on a re-demo. Raises on failure; the caller records that as the
    agent's provisioning status rather than letting it kill the request.
    """
    accounts = _load()
    if agent_id not in accounts:
        raise KeyError(f"no account for '{agent_id}' -- call ensure_account first")

    address = accounts[agent_id]["address"]
    agent_sk = algosdk.mnemonic.to_private_key(accounts[agent_id]["mnemonic"])
    server_addr = accounts["server"]["address"]
    server_sk = algosdk.mnemonic.to_private_key(accounts["server"]["mnemonic"])

    client = _algod()
    tx_ids = []
    micro_algo, micro_usdc = _balances(client, address)

    # 1. ALGO for fees and minimum balance. Without this the opt-in below
    #    can't even pay its own fee.
    if micro_algo < int(algo * 1_000_000 * 0.9):
        _set_status(agent_id, "funding", "sending ALGO for fees")
        txn = PaymentTxn(
            sender=server_addr,
            sp=client.suggested_params(),
            receiver=address,
            amt=int(algo * 1_000_000),
        )
        txid = client.send_transaction(txn.sign(server_sk))
        wait_for_confirmation(client, txid, 6)
        tx_ids.append(txid)
        _set_status(agent_id, "funding", f"funded {algo} ALGO", [txid])

    # 2. Opt in to USDC -- a zero-amount self-transfer, signed by the new
    #    agent itself. Algorand won't let an account receive an ASA it
    #    hasn't opted into, so this is not optional.
    if micro_usdc is None:
        _set_status(agent_id, "funding", "opting in to testnet USDC")
        optin = AssetTransferTxn(
            sender=address,
            sp=client.suggested_params(),
            receiver=address,
            amt=0,
            index=USDC_ASA_ID,
        )
        txid = client.send_transaction(optin.sign(agent_sk))
        wait_for_confirmation(client, txid, 6)
        tx_ids.append(txid)
        _set_status(agent_id, "funding", "opted in to USDC", [txid])
        micro_usdc = 0

    # 3. Working capital, so the agent can actually pay for API calls.
    if micro_usdc < int(usdc * 1_000_000 * 0.9):
        _set_status(agent_id, "funding", "sending USDC working capital")
        xfer = AssetTransferTxn(
            sender=server_addr,
            sp=client.suggested_params(),
            receiver=address,
            amt=int(usdc * 1_000_000),
            index=USDC_ASA_ID,
        )
        txid = client.send_transaction(xfer.sign(server_sk))
        wait_for_confirmation(client, txid, 6)
        tx_ids.append(txid)

    final_algo, final_usdc = _balances(client, address)
    _set_status(
        agent_id,
        "ready",
        f"funded: {final_algo / 1e6:.3f} ALGO, {(final_usdc or 0) / 1e6:.2f} USDC",
        tx_ids,
    )
    return {"address": address, "tx_ids": tx_ids, "algo": final_algo, "usdc": final_usdc}


def provision_async(agent_id: str, on_done=None, **kwargs) -> None:
    """Run provision() off the request thread.

    The agent is registered in policy the instant the request lands, so it
    shows up in the dashboard immediately; this fills in its on-chain
    ability to pay a few seconds later. A failure here leaves a registered
    agent that can't settle -- which is the honest state of the world, and
    is surfaced as `provisioning.state == "failed"` rather than hidden.
    """
    _set_status(agent_id, "funding", "starting on-chain provisioning")

    def run():
        try:
            result = provision(agent_id, **kwargs)
            if on_done:
                on_done(agent_id, result, None)
        except Exception as e:
            _set_status(agent_id, "failed", f"{type(e).__name__}: {e}")
            if on_done:
                on_done(agent_id, None, e)

    threading.Thread(target=run, daemon=True, name=f"provision-{agent_id}").start()
