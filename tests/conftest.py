"""Shared pytest setup.

Several tests drive POST /spend through the real FastAPI app, which appends
audit events and, past a threshold, auto-anchors the chain head by
submitting a genuine Algorand testnet transaction (see common/anchor.py).
That's correct behaviour in production and completely unwanted in a unit
test: it's slow, it needs the network, and it spends fees. Disabling it
here is process-wide and set before any test module imports the app, so no
individual test has to remember to opt out.

Tests that specifically exercise anchoring logic (tests/test_audit_ledger.py)
call the pure functions -- note building/parsing, chain verification --
rather than anchor_now(), so they're unaffected by this.
"""

import os

os.environ.setdefault("ASPE_DISABLE_ANCHOR", "1")
