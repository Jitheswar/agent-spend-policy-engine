"""Unit tests for common/identity.py -- the /spend request-signing scheme.

Uses real Algorand key material from data/accounts.json (the same keys the
live system uses), exercised via algosdk.util.sign_bytes/verify_bytes --
these are pure crypto tests, no FastAPI/DB involved.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.avm_client import agent_address, agent_secret_key_b64, load_accounts  # noqa: E402
from common.identity import sign_request, verify_request  # noqa: E402

ACCOUNTS = load_accounts()
WEATHER_KEY = agent_secret_key_b64("agent_weather", ACCOUNTS)
WEATHER_ADDRESS = agent_address("agent_weather", ACCOUNTS)
ROGUE_KEY = agent_secret_key_b64("agent_rogue", ACCOUNTS)


def test_valid_signature_verifies():
    fields = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    ok, reason = verify_request(
        WEATHER_ADDRESS, "agent_weather", "weather", 0.01, fields["timestamp"], fields["nonce"], fields["signature"]
    )
    assert ok, reason


def test_signature_from_a_different_agents_key_is_rejected():
    """The core impersonation case: signing with agent_rogue's key but
    claiming to be agent_weather must not verify against agent_weather's
    address."""
    fields = sign_request(ROGUE_KEY, "agent_weather", "weather", 0.01)
    ok, reason = verify_request(
        WEATHER_ADDRESS, "agent_weather", "weather", 0.01, fields["timestamp"], fields["nonce"], fields["signature"]
    )
    assert not ok
    assert "signature does not match" in reason


def test_tampered_amount_is_rejected():
    """Signature covers amount_usd -- a signed request for $0.01 can't be
    replayed with a different amount in the body."""
    fields = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    ok, reason = verify_request(
        WEATHER_ADDRESS, "agent_weather", "weather", 0.05, fields["timestamp"], fields["nonce"], fields["signature"]
    )
    assert not ok


def test_tampered_action_is_rejected():
    fields = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    ok, reason = verify_request(
        WEATHER_ADDRESS, "agent_weather", "enrich", 0.01, fields["timestamp"], fields["nonce"], fields["signature"]
    )
    assert not ok


def test_expired_timestamp_is_rejected():
    fields = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    stale_timestamp = fields["timestamp"] - 3600  # signed for an hour ago
    ok, reason = verify_request(
        WEATHER_ADDRESS, "agent_weather", "weather", 0.01, stale_timestamp, fields["nonce"], fields["signature"]
    )
    assert not ok
    assert "window" in reason


def test_replay_of_a_valid_signature_is_rejected():
    fields = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    args = (
        WEATHER_ADDRESS, "agent_weather", "weather", 0.01,
        fields["timestamp"], fields["nonce"], fields["signature"],
    )
    first_ok, _ = verify_request(*args)
    assert first_ok
    second_ok, second_reason = verify_request(*args)
    assert not second_ok
    assert "replayed" in second_reason


def test_same_signature_different_nonce_are_independent():
    """Sanity check that the replay guard keys on (agent_id, nonce), not
    just the agent -- two distinct real requests from the same agent must
    both succeed."""
    fields_a = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    fields_b = sign_request(WEATHER_KEY, "agent_weather", "weather", 0.01)
    assert fields_a["nonce"] != fields_b["nonce"]

    ok_a, _ = verify_request(
        WEATHER_ADDRESS, "agent_weather", "weather", 0.01,
        fields_a["timestamp"], fields_a["nonce"], fields_a["signature"],
    )
    ok_b, _ = verify_request(
        WEATHER_ADDRESS, "agent_weather", "weather", 0.01,
        fields_b["timestamp"], fields_b["nonce"], fields_b["signature"],
    )
    assert ok_a and ok_b


def test_malformed_signature_does_not_raise():
    ok, reason = verify_request(WEATHER_ADDRESS, "agent_weather", "weather", 0.01, time.time(), "n", "not-base64!!!")
    assert not ok
    assert reason
