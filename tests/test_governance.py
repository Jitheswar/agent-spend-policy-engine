"""Tests for the governance controls layered on top of the spend limits:
the kill switch, request velocity, and human-in-the-loop approval holds.

None of these tests let a real payment happen. Every path exercised either
ends in a denial (which never reaches the resource server by design) or
parks the request for approval (which stops before the payment step). The
one test that covers releasing a hold monkeypatches the payment executor --
what's being tested there is the release logic, not x402, which
scripts/phase1_client.py proves against the live network instead.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.avm_client import agent_secret_key_b64, load_accounts  # noqa: E402
from common.identity import sign_request  # noqa: E402
from policy_engine import storage  # noqa: E402

_ACCOUNTS = load_accounts()


def signed_body(agent_id: str, action: str, amount_usd: float) -> dict:
    sk = agent_secret_key_b64(agent_id, _ACCOUNTS)
    fields = sign_request(sk, agent_id, action, amount_usd)
    return {"agent_id": agent_id, "action": action, "amount_usd": amount_usd, **fields}


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


def patch_agent(monkeypatch, agent_id: str, **overrides):
    """Overlays fields onto one agent's policy for the duration of a test,
    without writing to the real policy.json (which is a live demo artifact
    a test has no business mutating)."""
    from policy_engine import app as app_module

    real = app_module.policy_store.get_policy()
    patched = {
        **real,
        "agents": {**real["agents"], agent_id: {**real["agents"][agent_id], **overrides}},
    }
    monkeypatch.setattr(app_module.policy_store, "get_policy", lambda: patched)
    return patched


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_frozen_agent_is_denied(client, monkeypatch):
    patch_agent(monkeypatch, "agent_weather", frozen=True)
    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
    assert body["decision"] == "denied"
    assert body["code"] == "frozen"
    assert "kill switch" in body["reason"]


def test_freeze_beats_remaining_budget(client, monkeypatch):
    """A frozen agent with plenty of budget left must still be stopped.

    This is the point of a kill switch as opposed to a cap: it has to mean
    "stops now", not "stops when the money runs out". Asserting on the
    denial code rather than just "denied" is what makes the test able to
    tell those two apart.
    """
    from policy_engine import app as app_module

    patch_agent(monkeypatch, "agent_weather", frozen=True, daily_cap_usd=1000.0)
    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
    assert body["code"] == "frozen"
    # And it stopped early enough that no budget was ever reserved.
    assert storage.get_daily_spend("agent_weather", app_module._today()) == 0.0


def test_freeze_is_checked_after_identity_not_before(client, monkeypatch):
    """An impersonator hitting a frozen agent should be told their identity
    failed, not that the agent is frozen. Denial reasons leak information,
    and "this agent exists and is currently frozen" is not something an
    unauthenticated caller should be able to probe for."""
    patch_agent(monkeypatch, "agent_weather", frozen=True)
    rogue_sk = agent_secret_key_b64("agent_rogue", _ACCOUNTS)
    forged = {"agent_id": "agent_weather", "action": "weather", "amount_usd": 0.01}
    forged.update(sign_request(rogue_sk, "agent_weather", "weather", 0.01))

    body = client.post("/spend", json=forged).json()
    assert body["code"] == "identity_failed"


def test_freeze_endpoint_persists_and_is_audited(client, monkeypatch):
    from policy_engine import app as app_module

    calls = []
    monkeypatch.setattr(app_module.policy_store, "set_frozen", lambda a, f: calls.append((a, f)) or {"frozen": f})

    resp = client.post("/admin/agents/agent_rogue/freeze", json={"frozen": True})
    assert resp.status_code == 200
    assert calls == [("agent_rogue", True)]

    latest = storage.get_audit_events(limit=1)[0]
    assert latest["event_type"] == "agent.frozen"


def test_freezing_an_unknown_agent_is_404(client):
    resp = client.post("/admin/agents/agent_nobody/freeze", json={"frozen": True})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Velocity
# ---------------------------------------------------------------------------


def test_velocity_limit_stops_a_runaway_agent(client, monkeypatch):
    """A daily cap bounds total damage but not rate -- an agent in a retry
    loop can burn a day's budget in seconds with every individual call
    inside policy. This is the check for that.

    require_approval_above_usd=0 parks each accepted request instead of
    paying it, so the test exercises velocity without touching the network.
    """
    patch_agent(
        monkeypatch,
        "agent_weather",
        max_requests_per_minute=3,
        require_approval_above_usd=0.0,
        daily_cap_usd=1000.0,
    )

    decisions = [
        client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
        for _ in range(5)
    ]

    assert [d["decision"] for d in decisions[:3]] == ["awaiting_approval"] * 3
    assert [d["code"] for d in decisions[3:]] == ["over_velocity_limit"] * 2
    assert "3 requests in the last 60s" in decisions[3]["reason"]


def test_velocity_counts_denied_requests_too(client, monkeypatch):
    """A loop that's being denied every time is still a runaway loop. If
    only successful calls counted, an agent hammering a disallowed action
    would never trip the limiter."""
    patch_agent(monkeypatch, "agent_weather", max_requests_per_minute=2)

    for _ in range(2):
        body = client.post("/spend", json=signed_body("agent_weather", "enrich", 0.05)).json()
        assert body["code"] == "action_not_allowed"

    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
    assert body["code"] == "over_velocity_limit"


def test_no_velocity_limit_configured_means_unlimited(client, monkeypatch):
    """Absence of the field has to be permissive -- otherwise adding this
    control to the codebase would retroactively lock out every agent in an
    existing policy.json that predates it."""
    patch_agent(
        monkeypatch,
        "agent_weather",
        max_requests_per_minute=None,
        require_approval_above_usd=0.0,
        daily_cap_usd=1000.0,
    )
    decisions = [
        client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
        for _ in range(6)
    ]
    assert all(d["decision"] == "awaiting_approval" for d in decisions)


# ---------------------------------------------------------------------------
# Human-in-the-loop approval holds
# ---------------------------------------------------------------------------


def test_spend_above_threshold_is_parked_not_paid(client, monkeypatch):
    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.005)
    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()

    assert body["decision"] == "awaiting_approval"
    assert "held for human review" in body["reason"]

    holds = client.get("/approvals").json()
    assert [h["id"] for h in holds] == [body["request_id"]]


def test_spend_at_or_below_threshold_is_not_parked(client, monkeypatch):
    """Strictly `above`, not `at or above` -- an agent whose threshold
    equals its normal per-call price would otherwise need a human for every
    single routine call."""
    from policy_engine import app as app_module

    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.01)
    monkeypatch.setattr(
        app_module, "_execute_payment", lambda *a, **kw: {"decision": "approved", "reason": "stubbed", "log": {}}
    )
    body = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
    assert body["decision"] == "approved"


def test_a_hold_reserves_budget_while_it_waits(client, monkeypatch):
    """A parked request must keep holding its money. If it released the
    budget while waiting, a second request could be approved against the
    same funds and the cap would be exceeded the moment the human said yes.
    """
    patch_agent(
        monkeypatch, "agent_rogue", require_approval_above_usd=0.001, daily_cap_usd=0.02
    )

    first = client.post("/spend", json=signed_body("agent_rogue", "weather", 0.01)).json()
    assert first["decision"] == "awaiting_approval"

    second = client.post("/spend", json=signed_body("agent_rogue", "weather", 0.01)).json()
    assert second["decision"] == "awaiting_approval"

    third = client.post("/spend", json=signed_body("agent_rogue", "weather", 0.01)).json()
    assert third["code"] == "over_daily_cap"


def test_rejecting_a_hold_frees_its_budget(client, monkeypatch):
    patch_agent(monkeypatch, "agent_rogue", require_approval_above_usd=0.001, daily_cap_usd=0.01)

    held = client.post("/spend", json=signed_body("agent_rogue", "weather", 0.01)).json()
    blocked = client.post("/spend", json=signed_body("agent_rogue", "weather", 0.01)).json()
    assert blocked["code"] == "over_daily_cap"

    resp = client.post(f"/admin/holds/{held['request_id']}/reject")
    assert resp.json()["decision"] == "denied"

    # Budget is back.
    retry = client.post("/spend", json=signed_body("agent_rogue", "weather", 0.01)).json()
    assert retry["decision"] == "awaiting_approval"


def test_approving_a_hold_runs_the_payment(client, monkeypatch):
    from policy_engine import app as app_module

    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.005)
    held = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()

    executed = []

    def fake_execute(agent_id, action, action_cfg, reservation_id):
        executed.append((agent_id, action, reservation_id))
        return {"decision": "approved", "reason": "stubbed", "log": storage.get_request(reservation_id)}

    monkeypatch.setattr(app_module, "_execute_payment", fake_execute)

    resp = client.post(f"/admin/holds/{held['request_id']}/approve")
    assert resp.json()["decision"] == "approved"
    assert executed == [("agent_weather", "weather", held["request_id"])]


def test_freezing_an_agent_blocks_its_already_queued_holds(client, monkeypatch):
    """A hold can sit for as long as the reviewer takes. If the agent is
    frozen in the meantime, releasing the hold must not still let it spend
    -- otherwise the kill switch has a queue-shaped hole in it.
    """
    from policy_engine import app as app_module

    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.005)
    held = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()

    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.005, frozen=True)
    monkeypatch.setattr(app_module, "_execute_payment", lambda *a, **kw: pytest.fail("payment must not run"))

    body = client.post(f"/admin/holds/{held['request_id']}/approve").json()
    assert body["decision"] == "denied"
    assert "was frozen while this request was awaiting approval" in body["reason"]


def test_a_hold_cannot_be_approved_twice(client, monkeypatch):
    from policy_engine import app as app_module

    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.005)
    held = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
    monkeypatch.setattr(
        app_module,
        "_execute_payment",
        lambda agent_id, action, cfg, rid: {
            "decision": "approved",
            "reason": "stubbed",
            "log": storage.finalize(rid, "approved", "stubbed"),
        },
    )

    assert client.post(f"/admin/holds/{held['request_id']}/approve").status_code == 200
    second = client.post(f"/admin/holds/{held['request_id']}/approve")
    assert second.status_code == 409


def test_unknown_hold_is_404(client):
    assert client.post("/admin/holds/999999/approve").status_code == 404
    assert client.post("/admin/holds/999999/reject").status_code == 404


def test_approval_decisions_are_audited(client, monkeypatch):
    patch_agent(monkeypatch, "agent_weather", require_approval_above_usd=0.005)
    held = client.post("/spend", json=signed_body("agent_weather", "weather", 0.01)).json()
    client.post(f"/admin/holds/{held['request_id']}/reject")

    types = [e["event_type"] for e in storage.get_audit_events(limit=20)]
    assert "approval.rejected" in types
    assert storage.verify_chain()["ok"] is True
