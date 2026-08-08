"""Notarizes the audit ledger's hash-chain head onto Algorand testnet.

This is the piece that makes the audit trail worth putting on a chain at
all. `policy_engine/storage.py` keeps an append-only, hash-chained ledger,
which makes local tampering *detectable* -- but only by someone who already
knows what the hashes were supposed to be. An operator with write access to
the database can edit an event, recompute its hash, and cascade the
recomputation forward until the whole chain is self-consistent again. A
purely local audit log has no defence against its own administrator.

Anchoring closes that. Every so often the current chain head (seq + hash)
is written into the note field of a real Algorand testnet transaction. From
that moment the head is committed to a public ledger the operator has no
write access to. Rewriting local history now produces a chain whose
recomputed hash at that seq disagrees with the hash sitting in the block --
and anyone can check that themselves against a public indexer without
trusting this codebase, this database, or this operator. That is the whole
argument, and it is not one a Postgres table can make.

Note format (kept short, greppable, and versioned):

    ASPE1|<seq>|<64-hex chain head hash>

A 0-ALGO self-payment carries it: nothing of value moves, the only payload
is the note, and it costs the standard minimum fee (0.001 ALGO).
"""

import base64
import os
import threading
import time

import algosdk
import requests
from algosdk.transaction import PaymentTxn, wait_for_confirmation
from algosdk.v2client import algod

from common.avm_client import load_accounts
from policy_engine import storage

ALGOD_URL = os.getenv("ALGOD_URL", "https://testnet-api.algonode.cloud")
INDEXER_URL = os.getenv("INDEXER_URL", "https://testnet-idx.algonode.cloud")
EXPLORER_TX_URL = "https://lora.algokit.io/testnet/transaction/{}"

NOTE_PREFIX = "ASPE1"

# The account that pays the anchoring fee. Reusing `server` (the resource
# server's payee) rather than adding a fifth funded account: it's already
# funded with ALGO, and anchoring is an operator action, not an agent one.
ANCHOR_ACCOUNT = "server"

# Anchor automatically once this many events have piled up since the last
# one. Low enough that a demo produces several real anchors in a couple of
# minutes, high enough that a burst of traffic doesn't fire a transaction
# per request.
AUTO_ANCHOR_THRESHOLD = int(os.getenv("AUTO_ANCHOR_THRESHOLD", "8"))

# Only one anchor may be in flight at a time -- two concurrent anchors would
# race to read the same head and burn a second fee for no extra commitment.
_anchor_lock = threading.Lock()
_in_flight = threading.Event()

_last_error: str | None = None


def anchoring_disabled() -> bool:
    """Kills every outbound Algorand write from this module.

    Read at call time rather than import time so a test process can set it
    in a conftest without caring about import order. Tests exercise /spend
    for real, which appends audit events, which would otherwise cross the
    auto-anchor threshold and start submitting genuine testnet transactions
    from a unit-test run -- slow, and it spends real (testnet) ALGO on fees
    for no benefit.
    """
    return os.getenv("ASPE_DISABLE_ANCHOR") == "1"


def _algod() -> algod.AlgodClient:
    return algod.AlgodClient("", ALGOD_URL)


def build_note(seq: int, head_hash: str) -> str:
    return f"{NOTE_PREFIX}|{seq}|{head_hash}"


def parse_note(note: str) -> tuple[int, str] | None:
    """Inverse of build_note. Returns None for anything that isn't one of
    our anchors -- the note field is free-form and an address's history can
    contain notes written by anything at all."""
    parts = note.split("|")
    if len(parts) != 3 or parts[0] != NOTE_PREFIX:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def anchor_now() -> dict:
    """Writes the current chain head to Algorand and records the anchor.

    Returns a result dict with "status": "anchored" | "nothing_to_anchor" |
    "busy" | "error". Never raises -- this runs on a background thread and
    from a request handler, and a failed anchor must degrade the integrity
    guarantee rather than take down spending.
    """
    global _last_error

    if anchoring_disabled():
        return {"status": "disabled", "detail": "ASPE_DISABLE_ANCHOR=1"}
    if not _anchor_lock.acquire(blocking=False):
        return {"status": "busy", "detail": "an anchor is already in flight"}
    _in_flight.set()
    try:
        head = storage.get_chain_head()
        if head is None:
            return {"status": "nothing_to_anchor", "detail": "the audit ledger is empty"}

        latest = storage.get_latest_anchor()
        if latest and latest["seq"] == head["seq"]:
            return {
                "status": "nothing_to_anchor",
                "detail": f"chain head (seq {head['seq']}) is already anchored",
                "anchor": latest,
            }

        seq, head_hash = head["seq"], head["entry_hash"]
        note = build_note(seq, head_hash)

        accounts = load_accounts()
        info = accounts[ANCHOR_ACCOUNT]
        sk = info["avm_private_key_b64"]
        address = algosdk.encoding.encode_address(base64.b64decode(sk)[32:])

        client = _algod()
        params = client.suggested_params()
        txn = PaymentTxn(
            sender=address,
            sp=params,
            receiver=address,  # 0-ALGO self-payment: the note is the entire point
            amt=0,
            note=note.encode(),
        )
        tx_id = client.send_transaction(txn.sign(sk))
        wait_for_confirmation(client, tx_id, 6)

        anchor = storage.record_anchor(seq, head_hash, tx_id, EXPLORER_TX_URL.format(tx_id), note)
        _last_error = None
        return {"status": "anchored", "anchor": anchor}
    except Exception as e:
        _last_error = str(e)
        return {"status": "error", "detail": str(e)}
    finally:
        _in_flight.clear()
        _anchor_lock.release()


def anchor_in_background() -> None:
    """Fire-and-forget anchor. Used by the auto-anchor hook so a /spend call
    never waits on an Algorand round trip it doesn't need."""
    if _in_flight.is_set() or anchoring_disabled():
        return
    threading.Thread(target=anchor_now, name="anchor", daemon=True).start()


def maybe_auto_anchor() -> None:
    """Called after each decision. Anchors once enough events have piled up."""
    if anchoring_disabled():
        return
    try:
        if storage.unanchored_event_count() >= AUTO_ANCHOR_THRESHOLD:
            anchor_in_background()
    except Exception:
        # Auto-anchoring is a background integrity nicety. If it can't even
        # count events, that's worth not crashing a spend decision over --
        # the explicit /audit/anchor endpoint still surfaces the real error.
        pass


def fetch_onchain_note(tx_id: str, timeout: int = 12) -> tuple[str | None, str]:
    """Reads an anchor's note back off the PUBLIC Algorand indexer.

    Deliberately a plain HTTP call to a public endpoint rather than anything
    routed through this project's own code: the entire value of the check is
    that the hash comes from somewhere the operator of this system can't
    write to. Returns (note_text_or_None, status_string).
    """
    url = f"{INDEXER_URL}/v2/transactions/{tx_id}"
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception as e:
        return None, f"could not reach the indexer: {e}"
    if resp.status_code == 404:
        # Normal for a few seconds after submission -- the indexer trails
        # the node. Distinguished from a real failure because "not indexed
        # yet" is not evidence of anything being wrong.
        return None, "not yet indexed (the public indexer trails the node by a few seconds)"
    if not resp.ok:
        return None, f"indexer returned {resp.status_code}"
    try:
        note_b64 = resp.json()["transaction"].get("note")
    except Exception as e:
        return None, f"unexpected indexer response shape: {e}"
    if not note_b64:
        return None, "transaction carries no note field"
    try:
        return base64.b64decode(note_b64).decode(), "ok"
    except Exception as e:
        return None, f"note is not decodable text: {e}"


def verify_anchor(anchor: dict, check_onchain: bool = True) -> dict:
    """Checks one anchor two ways.

    1. LOCAL: recompute the chain head as it stands *now* at the anchored
       seq, and compare to the hash the anchor recorded. A mismatch means
       the ledger was rewritten after this anchor was taken.
    2. ON-CHAIN: pull the note back off the public indexer and confirm it
       still says what we think it says. This is what makes (1) meaningful
       -- without it, an attacker who rewrote the ledger could also rewrite
       the locally-stored anchor hash to match.

    Only when both agree is the ledger genuinely proven unmodified since
    this anchor's block was written.
    """
    result = {
        "tx_id": anchor["tx_id"],
        "seq": anchor["seq"],
        "recorded_hash": anchor["head_hash"],
        "explorer_url": anchor.get("explorer_url"),
        "timestamp": anchor["timestamp"],
        "local_match": None,
        "onchain_match": None,
        "onchain_status": "not checked",
        "onchain_hash": None,
        "ok": False,
        "detail": "",
    }

    recomputed = storage.hash_at_seq(anchor["seq"])
    result["recomputed_hash"] = recomputed
    if recomputed is None:
        result["local_match"] = False
        result["detail"] = (
            f"the ledger no longer reaches seq {anchor['seq']} -- events at or before "
            "the anchored point were deleted"
        )
    else:
        result["local_match"] = recomputed == anchor["head_hash"]
        if not result["local_match"]:
            result["detail"] = (
                f"the ledger now hashes to {recomputed[:16]}… at seq {anchor['seq']}, but this "
                f"anchor committed {anchor['head_hash'][:16]}… to Algorand -- history was rewritten "
                "after it was notarized"
            )

    if check_onchain:
        note, status = fetch_onchain_note(anchor["tx_id"])
        result["onchain_status"] = status
        if note is not None:
            parsed = parse_note(note)
            if parsed is None:
                result["onchain_match"] = False
                result["detail"] = result["detail"] or f"on-chain note is not an anchor note: {note[:64]!r}"
            else:
                onchain_seq, onchain_hash = parsed
                result["onchain_hash"] = onchain_hash
                result["onchain_match"] = (
                    onchain_seq == anchor["seq"] and onchain_hash == anchor["head_hash"]
                )
                if not result["onchain_match"]:
                    result["detail"] = (
                        f"the anchor stored locally says seq {anchor['seq']}/{anchor['head_hash'][:16]}…, "
                        f"but the Algorand block says seq {onchain_seq}/{onchain_hash[:16]}… "
                        "-- the local anchor record was altered"
                    )

    # An unreachable/lagging indexer is explicitly NOT a verification
    # failure. Saying "tamper detected" because a public API was slow would
    # make the alert worthless in exactly the situation it matters.
    onchain_ok = result["onchain_match"] is not False
    result["ok"] = bool(result["local_match"]) and onchain_ok
    if result["ok"] and not result["detail"]:
        if result["onchain_match"]:
            result["detail"] = (
                f"seq {anchor['seq']} matches the hash recorded in Algorand block "
                f"transaction {anchor['tx_id'][:10]}…"
            )
        else:
            result["detail"] = f"local chain matches; on-chain check pending ({result['onchain_status']})"
    return result


def integrity_report(check_onchain: bool = True, anchor_limit: int = 5) -> dict:
    """The full picture: chain self-consistency plus every recent anchor.

    `verdict` collapses it to one of three states for display:
      - "verified"   -- chain intact and at least one anchor confirmed on-chain
      - "unanchored" -- chain intact but nothing notarized yet (or the
                        indexer hasn't caught up), so it's trustworthy only
                        as far as you trust this database
      - "tampered"   -- something doesn't add up; `detail` says what
    """
    chain = storage.verify_chain()
    anchors = [verify_anchor(a, check_onchain=check_onchain) for a in storage.get_anchors(limit=anchor_limit)]

    failed = [a for a in anchors if not a["ok"]]
    confirmed = [a for a in anchors if a["ok"] and a["onchain_match"]]

    if not chain["ok"]:
        verdict, detail = "tampered", chain["detail"]
    elif failed:
        verdict, detail = "tampered", failed[0]["detail"]
    elif confirmed:
        verdict, detail = "verified", confirmed[0]["detail"]
    elif anchors:
        verdict, detail = "unanchored", f"anchored, awaiting on-chain confirmation ({anchors[0]['onchain_status']})"
    else:
        verdict, detail = "unanchored", "no anchor written yet -- the ledger is not yet notarized on-chain"

    return {
        "verdict": verdict,
        "detail": detail,
        "chain": chain,
        "anchors": anchors,
        "unanchored_events": storage.unanchored_event_count(),
        "auto_anchor_threshold": AUTO_ANCHOR_THRESHOLD,
        "last_error": _last_error,
    }
