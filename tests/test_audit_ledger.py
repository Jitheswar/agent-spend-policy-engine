"""Tests for the append-only, hash-chained audit ledger and its anchors.

The claim this file exists to defend is specific: you can detect after the
fact that the audit trail was edited, even by whoever runs the database.
That splits into three testable parts, and all three matter --

  1. The chain links correctly and verify_chain() accepts an honest ledger.
  2. verify_chain() REJECTS a doctored ledger, including the competent case
     where the attacker recomputed the edited event's own hash. A tamper
     test that only covers the lazy attacker proves almost nothing.
  3. hash_at_seq() -- the function an anchor is compared against -- changes
     when history changes. That's the whole mechanism by which a hash
     sitting in an Algorand block catches a rewrite.

Anchoring itself (submitting the transaction) isn't tested here: it needs
the network and a funded account. The note format it depends on IS tested,
since a silent change there would break every existing anchor's
verification while every unit test stayed green.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import anchor  # noqa: E402
from policy_engine import storage  # noqa: E402


@pytest.fixture
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db()
    yield path
    if os.path.exists(path):
        os.remove(path)


def seed(n: int = 5) -> None:
    for i in range(n):
        storage.log_request(f"agent_{i}", "weather", 0.01 * (i + 1), "denied", f"reason {i}")


# ---------------------------------------------------------------------------
# The honest case
# ---------------------------------------------------------------------------


def test_empty_ledger_verifies(ledger):
    report = storage.verify_chain()
    assert report["ok"] is True
    assert report["event_count"] == 0
    assert report["head_seq"] == 0


def test_chain_links_each_event_to_its_predecessor(ledger):
    seed(4)
    events = sorted(storage.get_audit_events(limit=100), key=lambda e: e["seq"])
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[0]["prev_hash"] == storage.GENESIS_HASH
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["entry_hash"]
    assert storage.verify_chain()["ok"] is True


def test_every_decision_path_lands_in_the_ledger(ledger):
    """A reservation that later resolves must leave BOTH events behind.

    The operational `requests` row is updated in place -- the 'pending'
    state is gone the moment it resolves. If the ledger only recorded the
    final state, the record of money having been committed before it was
    settled would vanish with it, which is exactly the kind of gap an audit
    trail exists to not have.
    """
    row = storage.try_reserve("agent_a", "weather", 0.01, daily_cap_usd=1.0)
    storage.finalize(row["id"], "approved", "settled", tx_id="ABC123")

    events = sorted(storage.get_audit_events(limit=100), key=lambda e: e["seq"])
    types = [e["event_type"] for e in events]
    assert types == ["request.reserved", "request.decided"]

    decided = json.loads(events[1]["payload"])
    assert decided["decision"] == "approved"
    assert decided["tx_id"] == "ABC123"
    assert storage.verify_chain()["ok"] is True


def test_reset_appends_rather_than_truncating(ledger):
    """`POST /admin/reset` clears the demo log. If it could also clear the
    ledger, the ledger would be worthless -- so the wipe is itself an
    event, and everything before it stays."""
    seed(3)
    before = storage.get_event_count()

    storage.reset()

    assert storage.get_requests() == []
    assert storage.get_event_count() == before + 1
    latest = storage.get_audit_events(limit=1)[0]
    assert latest["event_type"] == "audit.reset"
    assert json.loads(latest["payload"])["requests_cleared"] == 3
    assert storage.verify_chain()["ok"] is True


# ---------------------------------------------------------------------------
# The dishonest cases
# ---------------------------------------------------------------------------


def test_detects_a_lazy_tamper_payload_edited_hash_left_alone(ledger):
    seed(5)
    with storage._connect() as conn:
        conn.execute(
            "UPDATE audit_events SET payload = ? WHERE seq = 3",
            (storage.canonical_payload({"agent_id": "agent_x", "amount_usd": 999.0}),),
        )

    report = storage.verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] == 3
    assert "modified in place" in report["detail"]


def test_detects_a_competent_tamper_payload_and_own_hash_both_rewritten(ledger):
    """The attacker recomputed the edited event's hash so it's internally
    consistent. It still fails, because event 4 commits to what event 3
    used to hash to."""
    seed(5)
    storage.tamper_with_event(3, {"amount_usd": 999.0})

    # The doctored event is now self-consistent -- confirm that, so this
    # test is demonstrably exercising the harder case and not the easy one.
    row = [e for e in storage.get_audit_events(limit=100) if e["seq"] == 3][0]
    assert row["entry_hash"] == storage.compute_entry_hash(
        row["seq"], row["timestamp"], row["event_type"], row["payload"], row["prev_hash"]
    )

    report = storage.verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] == 4
    assert "history was rewritten" in report["detail"]


def test_detects_a_deleted_event(ledger):
    seed(5)
    with storage._connect() as conn:
        conn.execute("DELETE FROM audit_events WHERE seq = 3")

    report = storage.verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] == 4
    assert "deleted" in report["detail"]


def test_a_fully_rewritten_chain_passes_locally_but_breaks_its_anchor(ledger):
    """The case that justifies putting anything on a chain at all.

    An attacker who rewrites an event AND recomputes every hash after it
    produces a ledger that verify_chain() cannot fault -- local hashing has
    no answer to someone who can rewrite all of it. What they cannot rewrite
    is the hash already sitting in an Algorand block. So after a full
    rewrite the ledger looks pristine, and the previously-anchored head
    hash no longer matches what the ledger now computes.
    """
    seed(5)
    anchored_seq = 5
    anchored_head = storage.get_chain_head()["entry_hash"]

    # Rewrite event 2 and cascade the recomputation all the way to the head.
    with storage._connect() as conn:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY seq ASC").fetchall()
        prev_hash = storage.GENESIS_HASH
        for row in rows:
            payload = row["payload"]
            if row["seq"] == 2:
                payload = storage.canonical_payload({"agent_id": "agent_1", "amount_usd": 0.01})
            new_hash = storage.compute_entry_hash(
                row["seq"], row["timestamp"], row["event_type"], payload, prev_hash
            )
            conn.execute(
                "UPDATE audit_events SET payload = ?, prev_hash = ?, entry_hash = ? WHERE seq = ?",
                (payload, prev_hash, new_hash, row["seq"]),
            )
            prev_hash = new_hash

    # Local verification is fooled -- honestly reported, because it is.
    assert storage.verify_chain()["ok"] is True

    # The anchor is not.
    assert storage.hash_at_seq(anchored_seq) != anchored_head
    result = anchor.verify_anchor(
        {
            "tx_id": "FAKE_TX_NOT_SUBMITTED",
            "seq": anchored_seq,
            "head_hash": anchored_head,
            "timestamp": "2026-08-08T00:00:00+00:00",
            "explorer_url": None,
        },
        check_onchain=False,
    )
    assert result["local_match"] is False
    assert result["ok"] is False
    assert "rewritten" in result["detail"]


def test_hash_at_seq_reproduces_the_head_of_an_untouched_ledger(ledger):
    seed(6)
    head = storage.get_chain_head()
    assert storage.hash_at_seq(head["seq"]) == head["entry_hash"]
    assert storage.hash_at_seq(999) is None


# ---------------------------------------------------------------------------
# Anchor note format
# ---------------------------------------------------------------------------


def test_note_round_trips():
    head_hash = "a" * 64
    note = anchor.build_note(42, head_hash)
    assert note == f"ASPE1|42|{head_hash}"
    assert anchor.parse_note(note) == (42, head_hash)


def test_note_stays_within_algorands_1kb_limit():
    assert len(anchor.build_note(10**9, "f" * 64).encode()) < 1024


@pytest.mark.parametrize(
    "note",
    [
        "",
        "hello world",
        "ASPE1|notanint|" + "a" * 64,
        "OTHER|1|" + "a" * 64,
        "ASPE1|1",
    ],
)
def test_parse_note_rejects_foreign_notes(note):
    """The note field is free-form and an account's history can contain
    anything. Verification must ignore notes that aren't ours rather than
    misreading one as a corrupt anchor."""
    assert anchor.parse_note(note) is None


def test_verify_anchor_treats_an_unreachable_indexer_as_inconclusive(ledger, monkeypatch):
    """A slow or down public indexer must never render as "tampered".

    Crying tamper because a third-party API timed out would make the alert
    worthless in exactly the moment it needs to be trusted -- so an
    unavailable indexer leaves the on-chain half unproven and says so,
    while the local half still reports honestly.
    """
    seed(3)
    head = storage.get_chain_head()
    monkeypatch.setattr(
        anchor, "fetch_onchain_note", lambda tx_id, timeout=12: (None, "could not reach the indexer: timeout")
    )

    result = anchor.verify_anchor(
        {
            "tx_id": "SOME_TX",
            "seq": head["seq"],
            "head_hash": head["entry_hash"],
            "timestamp": head["timestamp"],
            "explorer_url": None,
        }
    )
    assert result["local_match"] is True
    assert result["onchain_match"] is None
    assert result["ok"] is True
    assert "indexer" in result["onchain_status"]


def test_verify_anchor_catches_an_altered_local_anchor_record(ledger, monkeypatch):
    """Rewriting the ledger means also rewriting the locally-stored anchor
    hash to match, or step 1 catches you. Step 2 is what makes that
    pointless: the real hash is read back off the public chain."""
    seed(3)
    head = storage.get_chain_head()
    real_note = anchor.build_note(head["seq"], head["entry_hash"])
    monkeypatch.setattr(anchor, "fetch_onchain_note", lambda tx_id, timeout=12: (real_note, "ok"))

    forged_hash = "b" * 64
    result = anchor.verify_anchor(
        {
            "tx_id": "SOME_TX",
            "seq": head["seq"],
            "head_hash": forged_hash,
            "timestamp": head["timestamp"],
            "explorer_url": None,
        }
    )
    assert result["onchain_match"] is False
    assert result["ok"] is False
    assert "the local anchor record was altered" in result["detail"]


# ---------------------------------------------------------------------------
# The demo tamper endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(ledger):
    from fastapi.testclient import TestClient
    from policy_engine import app as app_module

    return TestClient(app_module.app)


def test_tamper_always_changes_the_value_it_rewrites(client, ledger):
    """The bug this guards against made the tamper demo silently no-op.

    The endpoint used to default to writing amount_usd = 0.01. Whenever the
    record it picked already held 0.01 -- which is the price of the demo's
    most-fired action -- it rewrote the value to itself. No bytes changed,
    so no hash changed, so verification correctly reported an intact chain
    and the tamper detector looked broken while actually being right.
    """
    storage.log_request("agent_weather", "weather", 0.01, "approved", "settled")
    storage.log_request("agent_weather", "weather", 0.01, "denied", "later event, so the edit is catchable")

    body = client.post("/admin/demo/tamper", json={"field": "amount_usd"}).json()

    assert body["original"]["amount_usd"] == 0.01
    assert body["replaced_with"]["amount_usd"] != 0.01
    assert storage.verify_chain()["ok"] is False


def test_tamper_avoids_the_head_event_because_that_edit_is_undetectable(client, ledger):
    """A hash chain cannot catch an in-place edit of its own last event:
    nothing follows it to disagree, and the tamperer recomputes its hash.
    So the demo endpoint must not pick the head when anything else is
    available -- otherwise it would "prove" the detector by triggering the
    one case the detector genuinely cannot see."""
    storage.log_request("agent_a", "enrich", 0.08, "approved", "pricey, but not the head")
    storage.log_request("agent_b", "weather", 0.01, "approved", "head event")

    head_seq = storage.get_chain_head()["seq"]
    body = client.post("/admin/demo/tamper", json={"field": "amount_usd"}).json()

    assert body["seq"] != head_seq
    assert storage.verify_chain()["ok"] is False


def test_editing_the_head_event_is_caught_only_by_an_anchor(ledger):
    """The honest statement of the limit above, and of what closes it.

    Anchoring the head commits it to a block. After that, editing it is
    caught -- not by the chain, which still verifies, but by the anchor.
    """
    storage.log_request("agent_a", "enrich", 0.08, "approved", "settled")
    head = storage.get_chain_head()

    storage.tamper_with_event(head["seq"], {"amount_usd": 0.01})
    # Nothing follows it, so the chain is still internally consistent.
    assert storage.verify_chain()["ok"] is True

    storage.restore_tampered()
    storage.record_anchor(head["seq"], head["entry_hash"], "TX", None, "note")

    storage.tamper_with_event(head["seq"], {"amount_usd": 0.01})
    result = anchor.verify_anchor(
        {
            "tx_id": "TX",
            "seq": head["seq"],
            "head_hash": head["entry_hash"],
            "timestamp": head["timestamp"],
            "explorer_url": None,
        },
        check_onchain=False,
    )
    assert result["local_match"] is False


def test_tamper_targets_the_priciest_record(client, ledger):
    """An operator hiding a spend hides the expensive one. Picking that
    record is also what makes the demo legible -- "$0.08 became $0.01" is a
    motive; "some row changed" is not."""
    storage.log_request("agent_a", "weather", 0.01, "approved", "cheap")
    storage.log_request("agent_b", "enrich", 0.08, "approved", "pricey")
    storage.log_request("agent_c", "weather", 0.02, "approved", "middling")

    body = client.post("/admin/demo/tamper", json={"field": "amount_usd"}).json()
    assert body["original"]["amount_usd"] == 0.08


def test_tamper_then_restore_returns_the_chain_to_intact(client, ledger):
    storage.log_request("agent_a", "enrich", 0.05, "approved", "settled")
    storage.log_request("agent_a", "weather", 0.01, "denied", "later event")
    head_before = storage.get_chain_head()["entry_hash"]

    client.post("/admin/demo/tamper", json={"field": "amount_usd"})
    assert storage.verify_chain()["ok"] is False

    restored = client.post("/admin/demo/restore").json()
    assert restored["restored"]
    assert storage.verify_chain()["ok"] is True
    # Byte-exact, not a recompute -- a "repair" that rewrote hashes would be
    # indistinguishable from the attack it claims to undo.
    assert storage.get_chain_head()["entry_hash"] == head_before


def test_tamper_with_nothing_to_tamper_is_a_clear_error(client, ledger):
    resp = client.post("/admin/demo/tamper", json={"field": "amount_usd"})
    assert resp.status_code == 400
    assert "fire a spend first" in resp.json()["detail"]


def test_integrity_report_verdicts(ledger, monkeypatch):
    seed(3)
    monkeypatch.setattr(anchor, "fetch_onchain_note", lambda tx_id, timeout=12: (None, "not yet indexed"))

    # No anchor written at all.
    assert anchor.integrity_report()["verdict"] == "unanchored"

    head = storage.get_chain_head()
    storage.record_anchor(head["seq"], head["entry_hash"], "TX1", None, "note")

    # record_anchor appends its own audit event, so the head has moved past
    # the anchored seq -- the anchor still verifies against the seq it
    # actually committed to.
    report = anchor.integrity_report()
    assert report["verdict"] == "unanchored"  # local ok, chain confirmation still pending
    assert report["anchors"][0]["local_match"] is True

    storage.tamper_with_event(2, {"amount_usd": 42.0})
    assert anchor.integrity_report()["verdict"] == "tampered"
