"""Tests for the policy engine's decision logic and, specifically, the
daily-cap race fix in storage.try_reserve.

Deliberately does NOT test the approval path end-to-end -- that requires a
live facilitator call and funded Algorand testnet accounts (see
scripts/phase1_client.py for that proof instead). What's tested here is the
part that's pure logic and the part most worth protecting from regressions:
policy decisions, and the concurrency correctness of the daily cap.
"""

import concurrent.futures
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.avm_client import agent_secret_key_b64, load_accounts  # noqa: E402
from common.identity import sign_request  # noqa: E402
from policy_engine import storage  # noqa: E402

_ACCOUNTS = load_accounts()


def signed_body(agent_id: str, action: str, amount_usd: float, **extra) -> dict:
    """A real, validly-signed /spend body for an agent that actually has
    key material on file -- for tests that need to get PAST identity
    verification to exercise deeper logic (policy limits, payment-flow
    error handling, etc)."""
    sk = agent_secret_key_b64(agent_id, _ACCOUNTS)
    fields = sign_request(sk, agent_id, action, amount_usd)
    return {"agent_id": agent_id, "action": action, "amount_usd": amount_usd, **fields, **extra}


def unsigned_body(agent_id: str, action: str, **extra) -> dict:
    """Placeholder signature fields for tests where identity is irrelevant
    -- e.g. agent_id itself is unknown/malicious and gets rejected before
    the handler ever reaches the identity-verification step. Only valid
    to use when the test asserts a denial that happens strictly earlier
    in policy_engine.app.spend()'s check order than identity verification."""
    return {
        "agent_id": agent_id,
        "action": action,
        "amount_usd": 0.0,
        "timestamp": time.time(),
        "nonce": "n/a",
        "signature": "n/a",
        **extra,
    }


@pytest.fixture
def isolated_db(monkeypatch):
    """Points storage.DB_PATH at a fresh temp file so tests never touch the
    real demo audit log, and different tests never see each other's rows."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # storage.init_db() creates it fresh
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db()
    yield path
    if os.path.exists(path):
        os.remove(path)


def utc_today() -> str:
    """The UTC day try_reserve will actually stamp its rows with.

    Never hardcode a date here. A previous version of these tests passed a
    literal "2026-08-07" as both the reservation day and the day to query
    back -- which quietly stopped exercising anything the moment the real
    clock moved past it: try_reserve wrote rows stamped with the real today,
    the cap query summed a day with no rows in it, and every reservation was
    accepted because the cap always looked empty. Three tests, including the
    concurrency regression test, passed for one day and then silently
    inverted into testing nothing.
    """
    return datetime.now(timezone.utc).date().isoformat()


def test_try_reserve_accepts_within_cap(isolated_db):
    row, rejected = storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)
    assert rejected is None
    assert row["decision"] == "pending"
    assert storage.get_daily_spend("agent_a", utc_today()) == 0.01


def test_try_reserve_rejects_over_cap(isolated_db):
    storage.try_reserve("agent_a", "weather", 0.02, daily_cap_usd=0.02)
    row, rejected = storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)
    assert row is None
    # The caller needs the numbers for its denial message, and they have to
    # come out of the same transaction that made the decision -- re-reading
    # afterwards reports a total that may already have moved.
    assert rejected["code"] == "over_daily_cap"
    assert rejected["spent_today"] == pytest.approx(0.02)
    assert rejected["cap"] == 0.02
    # The rejected attempt wrote nothing -- spend is still exactly the first call's.
    assert storage.get_daily_spend("agent_a", utc_today()) == 0.02


def test_try_reserve_allows_spending_exactly_up_to_the_cap(isolated_db):
    """0.01 + 0.01 is 0.020000000000000004 in IEEE 754. Without a tolerance
    in the cap comparison, an agent whose cap is exactly two calls wide gets
    its second call denied roughly whenever the rounding lands high -- a
    "you have $0.00 of $0.02 left" bug that only shows up for some amounts.
    """
    assert storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)[0] is not None
    assert storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)[0] is not None
    assert storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)[0] is None


def test_velocity_is_enforced_inside_the_reservation(isolated_db):
    """The rate limit is decided in the same transaction as the cap.

    It used to be a separate count-then-act in the policy engine, which made
    the cap race-free and left the limiter -- the control built specifically
    to stop a runaway loop, i.e. the most concurrent thing an agent does --
    with a window between reading the count and writing the row.
    """
    for _ in range(3):
        storage.try_reserve("agent_a", "weather", 0.01, 10.0, max_requests_per_minute=3)

    row, rejected = storage.try_reserve("agent_a", "weather", 0.01, 10.0, max_requests_per_minute=3)
    assert row is None
    assert rejected["code"] == "over_velocity_limit"
    assert rejected["recent"] == 3 and rejected["limit"] == 3
    # Rejected on rate, not budget: there was plenty of cap left.
    assert storage.get_daily_spend("agent_a", utc_today()) == pytest.approx(0.03)


def test_velocity_denial_wins_over_the_cap_when_both_trip(isolated_db):
    """Which denial an operator sees decides where they go looking. "Going
    too fast" sends them to a retry loop; "out of budget" sends them to a
    cap that is doing its job."""
    storage.try_reserve("agent_a", "weather", 0.02, 0.02, max_requests_per_minute=1)
    _, rejected = storage.try_reserve("agent_a", "weather", 0.02, 0.02, max_requests_per_minute=1)
    assert rejected["code"] == "over_velocity_limit"


def test_finalize_denied_frees_the_reservation(isolated_db):
    """A reservation whose payment subsequently fails must stop counting
    toward the cap -- otherwise a failed payment permanently burns budget."""
    row, _ = storage.try_reserve("agent_a", "weather", 0.02, daily_cap_usd=0.02)
    storage.finalize(row["id"], "denied", "payment request failed: connection refused")
    assert storage.get_daily_spend("agent_a", utc_today()) == 0.0
    # Budget should be available again.
    retry, _ = storage.try_reserve("agent_a", "weather", 0.02, daily_cap_usd=0.02)
    assert retry is not None


def test_a_crashed_reservation_stops_holding_budget(isolated_db):
    """A 'pending' row is a payment in flight. Nothing finalizes one if the
    process is killed mid-payment, and it counts toward the cap forever --
    an agent could come back from a crash with part of its budget spoken for
    by a payment that never happened, curable only by wiping the log.
    """
    row, _ = storage.try_reserve("agent_a", "weather", 0.02, daily_cap_usd=0.02)
    assert storage.get_daily_spend("agent_a", utc_today()) == 0.02
    assert storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)[0] is None

    # Nothing in flight can be older than the outbound timeout: that is the
    # longest a /spend call can possibly still be waiting.
    reaped = storage.reap_stale_reservations(older_than_seconds=0)

    assert reaped == [row["id"]]
    assert storage.get_daily_spend("agent_a", utc_today()) == 0.0

    # Recorded, not silently repaired: an abandoned spend is a real thing
    # that happened to a real reservation, and a ledger that omits it has a
    # gap exactly where an operator would go looking.
    assert storage.verify_chain()["ok"] is True
    latest = json.loads(storage.get_audit_events(limit=1)[0]["payload"])
    assert latest["decision"] == "denied" and "abandoned" in latest["reason"]

    # And the released budget is genuinely spendable again.
    assert storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=0.02)[0] is not None


def test_reaping_leaves_live_reservations_alone(isolated_db):
    """A reservation younger than the timeout may still be settling. Freeing
    its budget would let a concurrent request double-spend against money
    that is already committed."""
    row, _ = storage.try_reserve("agent_a", "weather", 0.02, daily_cap_usd=0.02)
    assert storage.reap_stale_reservations(older_than_seconds=3600) == []
    assert storage.get_request(row["id"])["decision"] == "pending"


def test_concurrent_reservations_never_exceed_cap(isolated_db):
    """The actual regression test for the TOCTOU race: fire N reservations
    at the same agent simultaneously from real OS threads (matching how
    FastAPI runs sync `def spend(...)` handlers in a threadpool), where the
    cap only has room for 2 of them. Before the BEGIN IMMEDIATE fix, two
    threads could both read "0 spent" before either wrote, and both pass.
    """
    cap = 0.02
    amount = 0.01
    n_requests = 12  # cap has room for exactly 2 of these

    def attempt(_):
        return storage.try_reserve("agent_rogue", "weather", amount, cap)[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_requests) as pool:
        results = list(pool.map(attempt, range(n_requests)))

    accepted = [r for r in results if r is not None]
    assert len(accepted) == 2, f"expected exactly 2 acceptances, got {len(accepted)}"

    total_reserved = storage.get_daily_spend("agent_rogue", utc_today())
    assert total_reserved == pytest.approx(0.02)
    assert total_reserved <= cap + 1e-9


def test_concurrent_requests_never_exceed_the_velocity_limit(isolated_db):
    """The same race, for the rate limiter. 12 simultaneous requests against
    a limit of 3: before the check moved inside the transaction, threads
    could all read the same sub-limit count and all proceed."""
    limit = 3
    n_requests = 12

    def attempt(_):
        return storage.try_reserve(
            "agent_rogue", "weather", 0.01, 100.0, max_requests_per_minute=limit
        )[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_requests) as pool:
        results = list(pool.map(attempt, range(n_requests)))

    assert len([r for r in results if r is not None]) == limit


@pytest.fixture
def client(isolated_db):
    """A TestClient against the real app, pointed at the isolated DB set up
    by the isolated_db fixture (same storage module, same monkeypatch)."""
    from fastapi.testclient import TestClient
    from policy_engine import app as app_module

    return TestClient(app_module.app)


def test_unknown_agent_denied_before_any_reservation(client):
    # agent_nobody isn't in policy.json OR accounts.json -- rejected at the
    # very first check, before identity verification, so a placeholder
    # signature is fine (and is all that's possible: there's no real key
    # for an agent that doesn't exist).
    resp = client.post("/spend", json=unsigned_body("agent_nobody", "weather"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "denied"
    assert "unknown agent" in body["reason"]


def test_unapproved_action_denied(client):
    # agent_weather is only approved for "weather", not "enrich". This
    # check runs AFTER identity verification, so it needs a real signature
    # to get past that step and actually exercise the allowed-action check.
    resp = client.post("/spend", json=signed_body("agent_weather", "enrich", 0.05))
    body = resp.json()
    assert body["decision"] == "denied"
    assert "not approved for action" in body["reason"]


def test_unsigned_request_rejected_by_schema(client):
    """The literal ask: /spend must not accept a request with no proof of
    identity at all."""
    resp = client.post("/spend", json={"agent_id": "agent_weather", "action": "weather"})
    assert resp.status_code == 422


def test_impersonation_with_another_agents_key_denied(client):
    """Signed for real, just with the WRONG agent's key -- proves identity
    verification actually checks the key, not just "is there a signature
    string present." """
    rogue_sk = agent_secret_key_b64("agent_rogue", _ACCOUNTS)
    forged = {"agent_id": "agent_weather", "action": "weather", "amount_usd": 0.01}
    forged.update(sign_request(rogue_sk, "agent_weather", "weather", 0.01))
    resp = client.post("/spend", json=forged)
    body = resp.json()
    assert body["decision"] == "denied"
    assert "identity verification failed" in body["reason"]


def test_replayed_spend_request_denied(client):
    # Deliberately an amount that exceeds agent_weather's per-request limit
    # (0.02) -- identity verification (and the nonce-replay guard) runs
    # BEFORE the limit check, so this still fully exercises replay
    # detection while guaranteeing neither call reaches the real payment
    # step (which would mean a live 5-8s x402/Algorand round trip in what
    # should be a fast unit test).
    body = signed_body("agent_weather", "weather", 0.5)
    first = client.post("/spend", json=body).json()
    assert first["decision"] == "denied"
    assert "exceeds per-request limit" in first["reason"]

    second = client.post("/spend", json=body).json()
    assert second["decision"] == "denied"
    assert "replayed" in second["reason"]


def test_per_request_limit_denied(client, monkeypatch):
    from policy_engine import app as app_module

    # None of the shipped demo agents have price > per_request_limit for an
    # allowed action (the allowed-action check would trip first), so patch
    # in a policy shape that actually exercises this branch in isolation.
    real_policy = app_module.policy_store.get_policy()
    patched = {
        **real_policy,
        "agents": {
            **real_policy["agents"],
            "agent_weather": {**real_policy["agents"]["agent_weather"], "per_request_limit_usd": 0.001},
        },
    }
    monkeypatch.setattr(app_module.policy_store, "get_policy", lambda: patched)

    resp = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01))
    body = resp.json()
    assert body["decision"] == "denied"
    assert "exceeds per-request limit" in body["reason"]


def test_oversized_agent_id_rejected_by_schema(client):
    resp = client.post("/spend", json=unsigned_body("x" * 200, "weather"))
    assert resp.status_code == 422


def test_session_prep_failure_finalizes_as_denied_and_frees_cap(client, monkeypatch):
    """A reservation must never survive a spend() call as 'pending'.

    If building the payment session blows up (bad key, transient error --
    no HTTP request was ever made, so no money could have moved), the
    reservation must resolve to 'denied' and stop counting toward the
    daily cap. Otherwise a persistently broken agent silently burns its
    whole cap through 500s that never show up as "denied" anywhere.
    """
    from policy_engine import app as app_module

    def boom(agent_id):
        raise RuntimeError("bad key in accounts.json")

    monkeypatch.setattr(app_module, "_session_for", boom)

    resp = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01))
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "denied"
    assert "failed to prepare payment session" in body["reason"]

    day = app_module._today()
    assert storage.get_daily_spend("agent_weather", day) == 0.0

    # Repeatable: a persistently broken agent keeps getting denied cleanly,
    # never quietly eats its cap via leaked reservations. Fresh signed_body
    # call -- a distinct nonce, since the same signed body can't be reused.
    resp2 = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01))
    assert resp2.json()["decision"] == "denied"
    assert storage.get_daily_spend("agent_weather", day) == 0.0


def test_settlement_parse_failure_still_finalizes_as_approved(client, monkeypatch):
    """Once response.ok is true, the resource server has already run
    verify()+settle() -- money has moved regardless of what happens next.
    A non-ValueError exception while parsing the settlement details (the
    original bug: only ValueError was caught) must NOT finalize as
    'denied' -- that would misrepresent a real spend as a no-op and
    incorrectly free its budget back onto the daily cap.
    """
    import x402.http as x402_http_module
    from policy_engine import app as app_module

    fake_response = SimpleNamespace(
        ok=True, status_code=200, text="", headers={}, json=lambda: {"weather": "sunny"}
    )
    fake_session = SimpleNamespace(get=lambda url, headers=None, timeout=30: fake_response)
    monkeypatch.setattr(app_module, "_session_for", lambda agent_id: (fake_session, "FAKEADDR", object()))

    class ExplodingHTTPClient:
        def __init__(self, *a, **kw):
            pass

        def get_payment_settle_response(self, header_getter):
            raise RuntimeError("boom -- deliberately not a ValueError")

    monkeypatch.setattr(x402_http_module, "x402HTTPClientSync", ExplodingHTTPClient)

    resp = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01))
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["tx_id"] is None
    assert "settlement details unavailable" in body["reason"]

    # The spend must still count toward the cap -- it really happened.
    day = app_module._today()
    assert storage.get_daily_spend("agent_weather", day) == pytest.approx(0.01)


def test_stored_agent_id_is_not_pre_escaped(client):
    """The XSS fix lives in the dashboard's rendering (escapeHtml in
    app.js), not in storage -- storage must keep the raw string so the
    audit trail reflects exactly what was submitted. Documents that
    boundary so a future "helpful" sanitize-on-write change doesn't get
    added in the wrong layer, or silently relied upon in the other."""
    payload = "<img src=x onerror=alert(1)>"
    resp = client.post("/spend", json=unsigned_body(payload, "weather"))
    body = resp.json()
    assert body["log"]["agent_id"] == payload
