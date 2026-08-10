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
# Must comfortably exceed the policy engine's outbound request timeout --
# this token is minted once and used for BOTH the initial 402-triggering
# request and the paid retry, so it has to stay valid for the full round
# trip, not just the first leg.
#
# Derived from that timeout rather than hardcoded alongside it. It was 90
# against a then-60s timeout, and OUTBOUND_TIMEOUT_SECONDS is configurable:
# raising it past 90 produced tokens that expired mid-retry and surfaced as
# `policy_auth_failed` -- a payment-shaped error with a configuration cause,
# which is the worst kind to debug.
TOKEN_TTL_SECONDS = config.OUTBOUND_TIMEOUT_SECONDS + 30
HEADER_NAME = "X-Policy-Authorization"


def params_fingerprint(params: dict | None) -> str:
    """A stable hash of the call arguments a token authorizes.

    Same canonical encoding as everything else that has to be reproduced
    byte-for-byte on the other side of a wire: sorted keys, no whitespace.
    """
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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


def mint_token(agent_id: str, action: str, params: dict | None = None) -> str:
    """Called by the policy engine immediately after a spend clears policy,
    right before it calls the resource server.

    The token commits to the agent and to the exact call arguments, not just
    the action. Without the arguments, a token issued for "enrich Apple Inc."
    authorized enriching *anything* for its whole lifetime -- so anyone who
    obtained one in flight could redirect the call it paid for while every
    check downstream still passed. Same argument as putting params inside
    the /spend signature (see common/identity.canonical_message); this is
    the resource server's half of it.
    """
    payload = {
        "agent_id": agent_id,
        "action": action,
        "params": params_fingerprint(params),
        "exp": time.time() + TOKEN_TTL_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_SECRET, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(
    token: str, expected_action: str, params: dict | None = None
) -> tuple[bool, str, str | None]:
    """Returns (ok, reason, claimed_agent_id). reason is only meaningful
    when ok is False. claimed_agent_id is whatever the token says even on
    failure paths where parsing succeeded, purely for logging -- the
    resource server has no independent way to confirm a request's agent
    identity before payment settles (that only exists after x402
    settlement, deeper in the stack), so this checks "a policy-engine
    decision approved this exact call very recently," not "this specific
    agent is the one paying." That's what's needed to close the bypass this
    exists for; it is not a claim of agent-to-payment correlation.

    Note what deliberately ISN'T here: single use. The x402 flow issues the
    request twice -- once to draw the 402, once carrying the payment -- and
    the same token has to satisfy both, so burning it on first use would
    break every paid call. The TTL is the bound instead, and it is a real
    one: the window is one request's round trip, not a session.
    """
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        return False, "malformed token", None

    # Both sides encoded to bytes before comparing: hmac.compare_digest
    # raises TypeError on non-ASCII str, and this token arrives in an HTTP
    # header that Starlette decodes as latin-1 -- so a header carrying any
    # high byte would crash the resource server rather than being rejected
    # as the invalid token it is.
    expected_sig = hmac.new(_SECRET, payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("utf-8", "surrogateescape"), expected_sig.encode()):
        return False, "invalid signature", None

    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
    except Exception:
        return False, "malformed payload", None

    claimed_agent_id = payload.get("agent_id")

    if payload.get("action") != expected_action:
        return False, "token was not issued for this action", claimed_agent_id

    if not hmac.compare_digest(str(payload.get("params", "")), params_fingerprint(params)):
        return False, "token was not issued for these call arguments", claimed_agent_id

    if time.time() > payload.get("exp", 0):
        return False, "token expired", claimed_agent_id

    return True, "", claimed_agent_id
