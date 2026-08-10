"""Cryptographic agent identity for POST /spend.

Before this, agent_id in a /spend request was just a free-text string --
anyone who could reach the policy engine's port could claim to be
"agent_weather" with nothing to back it up. This makes /spend require a
signature over the request, using the SAME Algorand key each agent already
needs for x402 payment signing (via algosdk.util.sign_bytes/verify_bytes,
which exist specifically for signing arbitrary application data with an
Algorand key -- domain-separated from transaction signing, so a valid
signature here can't be replayed as a transaction signature or vice versa).
Verification is against the agent's known Algorand address, so it proves
"whoever sent this HTTP request controls that agent's private key" -- real
cryptographic proof, not an assertion.

Be upfront about the actual trust boundary this closes, not more: in this
demo, the policy engine already holds every agent's private key custodially
(see common/avm_client.py -- that's how it signs the agent's payments for
it). There is no separate process where each agent independently holds a
key nobody else can touch. agents/simulate.py signs for real, using key
material it already has legitimate access to, exactly like a genuine
autonomous agent process would. The dashboard's Fire buttons are a human
operator, not a separate cryptographic identity, so they go through
policy_engine's POST /admin/sign -- a clearly-labeled convenience endpoint
that signs with the same custodial keys, explicitly NOT something a real
external agent would ever call (it would sign locally and call /spend
directly, like agents/simulate.py does). What this DOES genuinely prove:
/spend rejects any caller outside this system who doesn't possess an
agent's private key -- closing "just POST any agent_id you want" from
anyone without access to this repo's keys.
"""

import base64
import json
import os
import threading
import time

import algosdk.util

REQUEST_TTL_SECONDS = 60

# In-memory replay guard: (agent_id, nonce) seen within the TTL window is
# rejected. Doesn't need to survive a restart -- a restarted process has no
# in-flight requests to protect against replays of anyway.
_seen_lock = threading.Lock()
_seen_nonces: dict[tuple[str, str], float] = {}


def canonical_message(
    agent_id: str, action: str, amount_usd: float, timestamp: float, nonce: str,
    params: dict | None = None,
) -> bytes:
    """The exact bytes signed and verified. Fixed field order and separator
    -- both sides must build byte-identical messages or every signature
    fails.

    `params` (the arguments the call is for: which city, which company) is
    covered by the signature, because without it a valid signature for
    "enrich, $0.05" would authorize enriching *any* company -- an attacker
    who can modify a request in flight could redirect the call while leaving
    the agent, action and amount untouched, and every check downstream would
    still pass. Sorted keys and compact separators make the encoding
    canonical, so two callers with the same params always produce the same
    bytes regardless of dict ordering.

    It's appended only when non-empty, so a paramless call signs exactly the
    bytes it did before this field existed. That keeps every signature
    already in flight, and every stored test vector, valid.
    """
    message = f"{agent_id}|{action}|{amount_usd}|{timestamp}|{nonce}"
    if params:
        message += "|" + json.dumps(params, sort_keys=True, separators=(",", ":"))
    return message.encode()


def sign_request(
    secret_key_b64: str, agent_id: str, action: str, amount_usd: float,
    params: dict | None = None,
) -> dict:
    """Returns the fields a caller attaches to a /spend request: timestamp,
    nonce, and signature. secret_key_b64 is the same base64 sk format
    already used for payment signing (see common/avm_client.py)."""
    timestamp = time.time()
    nonce = base64.urlsafe_b64encode(os.urandom(9)).decode()
    message = canonical_message(agent_id, action, amount_usd, timestamp, nonce, params)
    signature = algosdk.util.sign_bytes(message, secret_key_b64)
    return {"timestamp": timestamp, "nonce": nonce, "signature": signature}


def verify_request(
    address: str, agent_id: str, action: str, amount_usd: float, timestamp: float, nonce: str,
    signature: str, params: dict | None = None,
) -> tuple[bool, str]:
    """Returns (ok, reason). Checks signature validity, freshness, and
    replay -- all three matter: a valid-but-old signature or a valid
    signature replayed twice must not pass."""
    now = time.time()
    if abs(now - timestamp) > REQUEST_TTL_SECONDS:
        return False, "request timestamp outside allowed window (stale or clock-skewed)"

    message = canonical_message(agent_id, action, amount_usd, timestamp, nonce, params)
    try:
        valid = algosdk.util.verify_bytes(message, signature, address)
    except Exception:
        return False, "malformed signature"
    if not valid:
        return False, "signature does not match agent's address"

    key = (agent_id, nonce)
    with _seen_lock:
        _prune_expired(now)
        if key in _seen_nonces:
            return False, "replayed request (nonce already used)"
        _seen_nonces[key] = now

    return True, ""


def _prune_expired(now: float) -> None:
    # Called with _seen_lock held. Bounded by REQUEST_TTL_SECONDS, so this
    # dict never grows past roughly (requests per TTL window) entries.
    expired = [k for k, seen_at in _seen_nonces.items() if now - seen_at > REQUEST_TTL_SECONDS]
    for k in expired:
        del _seen_nonces[k]
