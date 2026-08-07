"""Shared helper for building an x402-paying Algorand client for a given agent.

Used by both the raw Phase 1 proof script and the policy engine's payment
step -- an agent that clears policy pays exactly the way the Phase 1 script
proved works: sign an atomic Algorand transaction group, hand it to the
facilitator via the x402 X-PAYMENT header, retry, get served.
"""

import base64
import json
import os

import algosdk
from x402 import x402ClientSync
from x402.http.clients.requests import x402_requests
from x402.mechanisms.avm.exact.register import register_exact_avm_client

ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.json")


class AlgorandSigner:
    """Implements the ClientAvmSigner protocol using algosdk."""

    def __init__(self, secret_key: bytes, address: str):
        self._secret_key = secret_key
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    def sign_transactions(self, unsigned_txns, indexes_to_sign):
        sk_b64 = base64.b64encode(self._secret_key).decode()
        result = []
        for i, txn_bytes in enumerate(unsigned_txns):
            if i in indexes_to_sign:
                txn = algosdk.encoding.msgpack_decode(base64.b64encode(txn_bytes).decode())
                signed = txn.sign(sk_b64)
                result.append(base64.b64decode(algosdk.encoding.msgpack_encode(signed)))
            else:
                result.append(None)
        return result


def load_accounts() -> dict:
    with open(ACCOUNTS_PATH) as f:
        return json.load(f)


def agent_address(agent_name: str, accounts: dict | None = None) -> str:
    accounts = accounts or load_accounts()
    secret_key = base64.b64decode(accounts[agent_name]["avm_private_key_b64"])
    return algosdk.encoding.encode_address(secret_key[32:])


def agent_secret_key_b64(agent_name: str, accounts: dict | None = None) -> str:
    """The base64 sk format algosdk expects for both transaction signing
    (Transaction.sign) and arbitrary-message signing (algosdk.util.sign_bytes)
    -- same key material, domain-separated by algosdk internally so a
    signature under one scheme can't be replayed as the other."""
    accounts = accounts or load_accounts()
    return accounts[agent_name]["avm_private_key_b64"]


def build_paying_session(agent_name: str, accounts: dict | None = None):
    """Returns (requests.Session, address, x402ClientSync) for this agent.

    The session transparently pays x402 402s as this agent; the client
    object is returned too since reading the settlement response after a
    successful call requires it (x402HTTPClientSync(client)).
    """
    accounts = accounts or load_accounts()
    secret_key = base64.b64decode(accounts[agent_name]["avm_private_key_b64"])
    address = algosdk.encoding.encode_address(secret_key[32:])

    signer = AlgorandSigner(secret_key, address)
    x402 = x402ClientSync()
    register_exact_avm_client(x402, signer)
    return x402_requests(x402), address, x402
