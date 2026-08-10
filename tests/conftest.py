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

# Admin auth off by default, so the existing suite can drive /admin routes
# (freeze, holds, onboarding, the tamper demo) without threading a bearer
# token through every request -- those tests are about what the routes DO.
# An empty ADMIN_TOKEN is the documented "no authentication" setting; see
# common/config.admin_token(). Enforcement itself is tested for real in
# tests/test_admin_auth.py, which switches it on.
#
# Set before any test module imports common.config, because config reads it
# once at import to decide between "generate a token" and "explicitly off".
os.environ.setdefault("ADMIN_TOKEN", "")
