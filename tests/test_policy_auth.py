"""Unit tests for common/policy_auth.py -- the resource-server bypass fix.

Pure tests against the HMAC token scheme, no HTTP involved (the live
403-on-bypass / 200-on-legitimate-flow behavior is proven end-to-end
against the real running services, not re-simulated here).
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import policy_auth  # noqa: E402


def test_valid_token_verifies_for_its_action():
    token = policy_auth.mint_token("agent_weather", "weather")
    ok, reason, agent_id = policy_auth.verify_token(token, "weather")
    assert ok, reason
    assert agent_id == "agent_weather"


def test_token_rejected_for_a_different_action():
    """Closes off using a token minted for one paid route to authorize a
    request to a different one."""
    token = policy_auth.mint_token("agent_weather", "weather")
    ok, reason, _ = policy_auth.verify_token(token, "enrich")
    assert not ok
    assert "not issued for this action" in reason


def test_tampered_token_rejected():
    token = policy_auth.mint_token("agent_weather", "weather")
    payload_b64, sig = token.rsplit(".", 1)
    tampered = f"{payload_b64}.{'0' * len(sig)}"
    ok, reason, _ = policy_auth.verify_token(tampered, "weather")
    assert not ok
    assert "invalid signature" in reason


def test_malformed_token_rejected():
    ok, reason, _ = policy_auth.verify_token("not-a-real-token", "weather")
    assert not ok
    assert "malformed" in reason


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(policy_auth, "TOKEN_TTL_SECONDS", -1)  # expires immediately
    token = policy_auth.mint_token("agent_weather", "weather")
    ok, reason, _ = policy_auth.verify_token(token, "weather")
    assert not ok
    assert "expired" in reason


def test_different_tokens_for_repeated_calls():
    """Each mint produces a fresh token (nonce differs) -- not required to
    be single-use by this module (that's a short-TTL-window tradeoff
    documented in policy_auth.py), but confirms mint isn't accidentally
    deterministic/cacheable in a way that would make tokens guessable."""
    t1 = policy_auth.mint_token("agent_weather", "weather")
    t2 = policy_auth.mint_token("agent_weather", "weather")
    assert t1 != t2


# ---------------------------------------------------------------------------
# The token commits to the call arguments, not just the action
# ---------------------------------------------------------------------------


def test_a_token_is_bound_to_the_arguments_it_was_issued_for():
    """Without this, a token minted for "enrich Apple Inc." authorized
    enriching ANY company for its whole lifetime -- so anyone who obtained
    one in flight could redirect the call it paid for while every other
    check downstream still passed. Same argument as putting params inside
    the /spend signature; this is the resource server's half of it."""
    token = policy_auth.mint_token("agent_enrichment", "enrich", {"company": "Apple Inc."})

    ok, _, _ = policy_auth.verify_token(token, "enrich", {"company": "Apple Inc."})
    assert ok

    ok, reason, _ = policy_auth.verify_token(token, "enrich", {"company": "Tesla"})
    assert not ok
    assert "not issued for these call arguments" in reason


def test_arguments_cannot_be_added_or_dropped():
    token = policy_auth.mint_token("agent_weather", "weather", {"city": "Oslo"})
    assert not policy_auth.verify_token(token, "weather", {})[0]
    assert not policy_auth.verify_token(token, "weather", {"city": "Oslo", "units": "F"})[0]


def test_key_order_does_not_matter():
    """The fingerprint is canonical, so two callers building the same
    arguments in a different order agree."""
    token = policy_auth.mint_token("agent_x", "enrich", {"a": "1", "b": "2"})
    assert policy_auth.verify_token(token, "enrich", {"b": "2", "a": "1"})[0]


def test_a_paramless_call_treats_none_and_empty_alike():
    """The engine omits the query string entirely when there are no
    arguments, so the resource server sees {} where the engine passed None.
    Those have to hash the same or every paramless paid call 403s."""
    token = policy_auth.mint_token("agent_weather", "weather", None)
    assert policy_auth.verify_token(token, "weather", {})[0]
    assert policy_auth.verify_token(token, "weather", None)[0]


def test_the_ttl_covers_the_engines_outbound_budget():
    """The token is minted once and used for BOTH the 402-triggering
    request and the paid retry, so it has to outlive the whole round trip.
    It was hardcoded at 90 against a configurable timeout: raising that
    past 90 produced tokens expiring mid-retry, which surfaced as
    `policy_auth_failed` -- a payment-shaped error with a config cause.
    """
    from common import config

    assert policy_auth.TOKEN_TTL_SECONDS > config.OUTBOUND_TIMEOUT_SECONDS
