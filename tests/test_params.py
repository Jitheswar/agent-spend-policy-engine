"""Call arguments as a governed thing.

Now that the paid APIs take arguments, "how much may this agent spend" is no
longer the whole question. An agent with an ordinary budget can still call a
sanctioned action with arguments nobody sanctioned, so params are declared
per action in policy.json, validated before any budget is touched, covered
by the request signature, and recorded in the ledger.

These cover the three separate claims that follow from that:
  - the schema is enforced (unknown keys, required keys, length)
  - the signature covers params, so they can't be swapped in flight
  - what got spent on *what* survives into the audit record
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.avm_client import agent_secret_key_b64, load_accounts  # noqa: E402
from common.identity import canonical_message, sign_request, verify_request  # noqa: E402
from policy_engine import storage  # noqa: E402

_ACCOUNTS = load_accounts()


def signed_body(agent_id: str, action: str, amount_usd: float, params: dict | None = None) -> dict:
    sk = agent_secret_key_b64(agent_id, _ACCOUNTS)
    fields = sign_request(sk, agent_id, action, amount_usd, params)
    body = {"agent_id": agent_id, "action": action, "amount_usd": amount_usd, **fields}
    if params is not None:
        body["params"] = params
    return body


@pytest.fixture
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db()
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def client(isolated_db):
    from fastapi.testclient import TestClient

    from policy_engine import app as app_module

    return TestClient(app_module.app)


@pytest.fixture
def no_payment(monkeypatch):
    """Stops short of the x402 leg. These tests are about what the engine
    decides and records, not about settling on testnet."""
    from policy_engine import app as app_module

    def fake_execute(agent_id, action, action_cfg, reservation_id, params=None):
        return {
            "decision": "approved",
            "reason": "stubbed",
            "params": params,
            "log": storage.finalize(reservation_id, "approved", "stubbed"),
        }

    monkeypatch.setattr(app_module, "_execute_payment", fake_execute)


# ---------------------------------------------------------------------------
# Schema enforcement
# ---------------------------------------------------------------------------


def test_a_default_fills_in_for_an_omitted_param(no_payment, client):
    """The dashboard's one-click Fire buttons send no params. Policy supplies
    the value, which is why they still work."""
    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()

    assert body["decision"] == "approved"
    assert body["params"] == {"city": "San Francisco"}


def test_a_param_no_policy_declares_is_denied(no_payment, client):
    """An action's accepted arguments are the operator's call. An undeclared
    key is a denial, not something quietly forwarded to the upstream."""
    body = client.post(
        "/spend", json=signed_body("agent_weather", "weather", 0.01, {"planet": "Mars"})
    ).json()

    assert body["decision"] == "denied"
    assert body["code"] == "invalid_params"
    assert "planet" in body["reason"]


def test_an_overlong_param_is_denied(no_payment, client):
    body = client.post(
        "/spend", json=signed_body("agent_weather", "weather", 0.01, {"city": "x" * 200})
    ).json()

    assert body["decision"] == "denied"
    assert body["code"] == "invalid_params"


def test_a_required_param_with_no_default_is_denied(no_payment, client, monkeypatch):
    from policy_engine import app as app_module

    real = app_module.policy_store.get_policy()
    patched = {
        **real,
        "actions": {
            **real["actions"],
            "weather": {**real["actions"]["weather"], "params": {"city": {"required": True}}},
        },
    }
    monkeypatch.setattr(app_module.policy_store, "get_policy", lambda: patched)

    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()

    assert body["decision"] == "denied"
    assert "missing required parameter 'city'" in body["reason"]


def test_invalid_params_are_denied_before_any_budget_moves(no_payment, client):
    """Ordering: a malformed call must not reserve money it can never spend.
    If validation ran after the cap reservation, a loop of bad requests would
    eat an agent's whole daily budget without a single payment attempt."""
    before = storage.get_daily_spend("agent_weather", storage._now()[:10])

    client.post("/spend", json=signed_body("agent_weather", "weather", 0.01, {"planet": "Mars"}))

    assert storage.get_daily_spend("agent_weather", storage._now()[:10]) == before


# ---------------------------------------------------------------------------
# The signature covers params
# ---------------------------------------------------------------------------


def test_swapping_params_in_flight_invalidates_the_signature(no_payment, client):
    """Without params in the signed message, a valid signature for
    "enrich, $0.05" would authorize enriching *any* company -- an attacker
    able to modify the request could redirect the call and leave agent,
    action and amount untouched."""
    body = signed_body("agent_enrichment", "enrich", 0.05, {"company": "Apple"})
    body["params"] = {"company": "Tesla"}

    result = client.post("/spend", json=body).json()

    assert result["decision"] == "denied"
    assert result["code"] == "identity_failed"


def test_params_are_canonicalized_so_key_order_does_not_matter():
    """Two callers building the same logical params must produce identical
    bytes, or signatures would fail based on dict ordering alone."""
    a = canonical_message("a", "act", 0.01, 123.0, "n", {"x": "1", "y": "2"})
    b = canonical_message("a", "act", 0.01, 123.0, "n", {"y": "2", "x": "1"})

    assert a == b


def test_a_paramless_call_signs_exactly_what_it_used_to():
    """Backward compatibility is deliberate: params are appended only when
    non-empty, so every signature that predates the field stays valid."""
    without = canonical_message("a", "act", 0.01, 123.0, "n")
    empty = canonical_message("a", "act", 0.01, 123.0, "n", {})
    none = canonical_message("a", "act", 0.01, 123.0, "n", None)

    assert without == empty == none
    assert without == b"a|act|0.01|123.0|n"


def test_verification_round_trips_with_params():
    from common.avm_client import agent_address

    params = {"city": "Reykjavik"}
    sk = agent_secret_key_b64("agent_weather", _ACCOUNTS)
    fields = sign_request(sk, "agent_weather", "weather", 0.01, params)

    ok, _ = verify_request(
        agent_address("agent_weather", _ACCOUNTS), "agent_weather", "weather", 0.01,
        fields["timestamp"], fields["nonce"], fields["signature"], params,
    )
    assert ok


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_the_ledger_records_what_the_money_was_spent_on(no_payment, client):
    """"$0.05 on enrich" is a much weaker audit record than "$0.05 to enrich
    Apple Inc." -- and the second is the one an operator needs when reading
    back what an agent did."""
    client.post(
        "/spend", json=signed_body("agent_enrichment", "enrich", 0.05, {"company": "Apple"})
    )

    decided = [
        json.loads(e["payload"])
        for e in storage.get_audit_events(limit=50)
        if e["event_type"] == "request.decided"
    ]

    assert decided, "expected a decision event"
    assert decided[0]["params"] == {"company": "Apple"}


def test_a_denial_records_the_params_it_rejected(no_payment, client):
    """Including the ones policy refused. What an agent *tried* to call is
    part of the record, not just what it managed to."""
    client.post("/spend", json=signed_body("agent_weather", "weather", 0.01, {"planet": "Mars"}))

    row = storage.get_requests(limit=1)[0]

    assert row["decision"] == "denied"
    assert json.loads(row["params"]) == {"planet": "Mars"}


def test_denials_raised_before_validation_still_record_the_attempt(no_payment, client):
    """These fire before the param schema is even consulted, so it would be
    easy for them to record nothing. "agent_weather tried to enrich Apple and
    was refused" is the sentence an operator needs; "agent_weather was
    refused" makes them go digging."""
    client.post(
        "/spend", json=signed_body("agent_weather", "enrich", 0.05, {"company": "Apple"})
    )

    row = storage.get_requests(limit=1)[0]

    assert row["decision"] == "denied"
    assert "not approved for action" in row["reason"]
    assert json.loads(row["params"]) == {"company": "Apple"}


def test_the_params_column_is_added_to_a_pre_existing_database(monkeypatch):
    """A database created before the column existed has to gain it on
    startup -- CREATE TABLE IF NOT EXISTS does nothing to a table that's
    already there, so without a migration the schema is only correct on a
    machine that started from an empty data/ directory."""
    import sqlite3

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
        "agent_id TEXT NOT NULL, action TEXT NOT NULL, amount_usd REAL NOT NULL, "
        "decision TEXT NOT NULL, reason TEXT NOT NULL, tx_id TEXT, explorer_url TEXT)"
    )
    conn.execute(
        "INSERT INTO requests (timestamp, agent_id, action, amount_usd, decision, reason) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'agent_weather', 'weather', 0.01, 'approved', 'old row')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db()

    try:
        row = storage.get_requests(limit=1)[0]
        assert row["params"] == "{}", "the pre-existing row should default, not break"
        storage.log_request("agent_weather", "weather", 0.01, "approved", "new", params={"city": "Oslo"})
        assert json.loads(storage.get_requests(limit=1)[0]["params"]) == {"city": "Oslo"}
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# The hard ceiling underneath the policy schema
# ---------------------------------------------------------------------------


def test_an_absurd_parameter_value_is_refused_outright(client):
    """A denial is written into an append-only ledger with no delete path.
    That is the point of the ledger, and it is also why an unauthenticated
    caller must not be able to choose how many bytes go into it: without a
    ceiling, one /spend request commits an arbitrary blob permanently, and
    drives auto-anchoring (and its transaction fees) on the way.

    422, not a policy denial: this is malformed input, not a governed call,
    so it is refused before anything is recorded rather than after.
    """
    before = storage.get_event_count()
    response = client.post(
        "/spend",
        json={
            "agent_id": "nobody",
            "action": "weather",
            "params": {"city": "x" * 200_000},
            "timestamp": 0,
            "nonce": "n",
            "signature": "s",
        },
    )

    assert response.status_code == 422
    assert storage.get_event_count() == before, "nothing may reach the ledger"


def test_too_many_parameters_are_refused(client):
    response = client.post(
        "/spend",
        json={
            "agent_id": "nobody",
            "action": "weather",
            "params": {f"k{i}": "v" for i in range(200)},
            "timestamp": 0,
            "nonce": "n",
            "signature": "s",
        },
    )
    assert response.status_code == 422


def test_an_absurd_parameter_name_is_refused(client):
    response = client.post(
        "/spend",
        json={
            "agent_id": "nobody",
            "action": "weather",
            "params": {"k" * 5_000: "v"},
            "timestamp": 0,
            "nonce": "n",
            "signature": "s",
        },
    )
    assert response.status_code == 422


def test_ordinary_arguments_are_nowhere_near_the_ceiling(no_payment, client):
    """The ceiling is anti-abuse, not governance: a real call must never
    come close to it, or it would be doing the policy schema's job badly."""
    body = signed_body("agent_weather", "weather", 0.01, {"city": "Reykjavik"})
    assert client.post("/spend", json=body).json()["decision"] == "approved"


def test_the_signing_helper_is_bounded_too(client):
    """/admin/sign takes the same params and would otherwise be a way to
    hand the engine an unbounded dict through a different door."""
    response = client.post(
        "/admin/sign",
        json={
            "agent_id": "agent_weather",
            "action": "weather",
            "amount_usd": 0.01,
            "params": {"city": "x" * 200_000},
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# List endpoints are bounded
# ---------------------------------------------------------------------------


def test_a_negative_limit_is_refused_rather_than_meaning_unlimited(client):
    """SQLite reads a negative LIMIT as "no limit", so ?limit=-1 returned
    the entire table -- and serialised all of it to JSON in memory."""
    for path in ("/requests", "/audit/events", "/audit/anchors"):
        assert client.get(f"{path}?limit=-1").status_code == 422, path


def test_an_enormous_limit_is_refused(client):
    for path in ("/requests", "/audit/events", "/audit/anchors"):
        assert client.get(f"{path}?limit=100000000").status_code == 422, path


def test_the_default_and_ordinary_limits_still_work(client):
    assert client.get("/requests").status_code == 200
    assert client.get("/requests?limit=50").status_code == 200
    assert client.get("/audit/events?limit=1000").status_code == 200
