"""One place that builds an algod client.

There were four of these, and three of them passed an empty token:

    algod.AlgodClient("", ALGOD_URL)          # anchor.py, provisioning.py,
                                              # redistribute_algo.py
    algod.AlgodClient(config.ALGOD_TOKEN, …)  # setup_accounts.py

Which meant `ALGOD_TOKEN` was resolved by common/config.py, documented in
.env.example, and then ignored by everything except account setup. Point
this at an authenticated node -- a private algod, a paid provider tier --
and `setup_accounts.py balances` works while anchoring and provisioning
fail, which reads as "the chain is broken" rather than "the token isn't
being sent."

Same argument as the network profile living in one module: a constant that
exists in four places has already drifted in at least one of them.
"""

from algosdk.v2client import algod

from common import config


def client() -> algod.AlgodClient:
    """An algod client pointed at the configured node, with the configured
    token. AlgoNode's public endpoints want an empty token, which is the
    default, so this is a no-op for the shipped configuration."""
    return algod.AlgodClient(config.ALGOD_TOKEN, config.ALGOD_URL)
