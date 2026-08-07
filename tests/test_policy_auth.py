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
