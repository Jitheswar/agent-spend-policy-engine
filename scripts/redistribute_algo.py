"""One-off: redistribute ALGO from one over-funded generated account to the others.

Sends `--amount` ALGO (default 2) from `--from` to every other generated
account that currently has less than 1 ALGO. All keys are in data/accounts.json
so no faucet/captcha is needed for this -- it's just an on-chain transfer
between accounts we already control.
"""

import argparse
import base64
import json
import os

import algosdk
from algosdk.v2client import algod
from algosdk.transaction import PaymentTxn

ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.json")
ALGOD_URL = "https://testnet-api.algonode.cloud"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_name", default="server")
    parser.add_argument("--amount", type=float, default=2.0, help="ALGO per recipient")
    args = parser.parse_args()

    with open(ACCOUNTS_PATH) as f:
        accounts = json.load(f)

    sender_info = accounts[args.from_name]
    sender_addr = sender_info["address"]
    sender_sk = algosdk.mnemonic.to_private_key(sender_info["mnemonic"])

    client = algod.AlgodClient("", ALGOD_URL)
    params = client.suggested_params()
    micro_amount = int(args.amount * 1_000_000)

    for name, info in accounts.items():
        if name == args.from_name:
            continue
        addr = info["address"]
        try:
            acc = client.account_info(addr)
            balance = acc.get("amount", 0)
        except Exception:
            balance = 0
        if balance >= 1_000_000:  # already has >= 1 ALGO
            print(f"[{name}] already has {balance/1_000_000:.3f} ALGO, skipping")
            continue
        txn = PaymentTxn(sender=sender_addr, sp=params, receiver=addr, amt=micro_amount)
        signed = txn.sign(sender_sk)
        txid = client.send_transaction(signed)
        algosdk.transaction.wait_for_confirmation(client, txid, 4)
        print(f"[{name}] funded with {args.amount} ALGO, txid={txid}")


if __name__ == "__main__":
    main()
