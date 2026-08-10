"""Shared-secret HMAC authorization between the policy engine and the
resource server.

Closes a real gap: the resource server's x402 middleware, on its own, will
happily sell /weather or /enrich to *anyone* who pays for it -- it has no
idea the policy engine exists. This module lets the policy engine mint a
short-lived token after a spend clears policy, which the resource server
verifies before it will even issue a 402, let alone accept payment. An
unauthorized direct call gets rejected outright.

Be clear about what this is and isn't: it's a shared static secret on the
local filesystem, verified with HMAC -- a demo-appropriate stand-in for a
real trust boundary. A production deployment would put the resource server
on a network only the policy engine can reach (or use mTLS) rather than
trust a secret both processes happen to read off the same disk. This still
genuinely closes the specific hole it's built for -- an external caller
with no access to this repo cannot forge a valid token -- it just doesn't
pretend to be a substitute for real network isolation.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from common import config

SECRET_PATH = config.POLICY_AUTH_SECRET_PATH
# Must comfortably exceed the policy engine's outbound request timeout
# (currently 60s, see policy_engine/app.py) -- this token is minted once
# and used for both the initial 402-triggering request and the paid retry,
# so it has to stay valid for the full round trip, not just the first leg.
TOKEN_TTL_SECONDS = 90
HEADER_NAME = "X-Policy-Authorization"


def _load_or_create_secret() -> bytes:
    os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH) as f:
            return bytes.fromhex(f.read().strip())
    secret = secrets.token_bytes(32)
    with open(SECRET_PATH, "w") as f:
        f.write(secret.hex())
    return secret


_SECRET = _load_or_create_secret()


def mint_token(agent_id: str, action: str) -> str:
    """Called by the policy engine immediately after a spend clears policy,
    right before it calls the resource server. Short TTL: this token is
    used within the same request, not stored or reused deliberately."""
    payload = {
        "agent_id": agent_id,
        "action": action,
        "exp": time.time() + TOKEN_TTL_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_SECRET, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(token: str, expected_action: str) -> tuple[bool, str, str | None]:
    """Returns (ok, reason, claimed_agent_id). reason is only meaningful
    when ok is False. claimed_agent_id is whatever the token says even on
    failure paths where parsing succeeded, purely for logging -- the
    resource server has no independent way to confirm a request's agent
    identity before payment settles (that only exists after x402
    settlement, deeper in the stack), so this checks "some policy-engine
    decision approved this action in the last 30 seconds," not "this
    specific agent." That's what's needed to close the bypass this exists
    for; it is not a claim of agent-to-payment correlation.
    """
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        return False, "malformed token", None

    expected_sig = hmac.new(_SECRET, payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False, "invalid signature", None

    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
    except Exception:
        return False, "malformed payload", None

    claimed_agent_id = payload.get("agent_id")

    if payload.get("action") != expected_action:
        return False, "token was not issued for this action", claimed_agent_id

    if time.time() > payload.get("exp", 0):
        return False, "token expired", claimed_agent_id

    return True, "", claimed_agent_id
