"""SQLite-backed audit trail for the policy engine.

Two tables, two very different jobs:

`requests` is the OPERATIONAL view -- one row per spend request, mutated in
place as a reservation resolves (pending -> approved/denied). It's what the
dashboard reads and what daily-cap arithmetic sums over. It is deliberately
mutable, and `POST /admin/reset` wipes it for demo rehearsals.

`audit_events` is the LEDGER -- append-only and hash-chained. Every state
transition that `requests` records mutably is also appended here immutably:
each event commits to the one before it (`prev_hash`), and to its own
contents (`entry_hash = sha256(seq|timestamp|type|payload|prev_hash)`), so
editing any historical event breaks every hash after it. Nothing in this
module ever updates or deletes an audit event -- not even reset(), which
appends an `audit.reset` event rather than truncating the ledger.

That alone only makes tampering *detectable by someone holding the original
hashes*. An operator who can edit the database can also recompute the whole
chain. What closes that is `common/anchor.py`: it periodically writes the
current chain head into the note field of a real Algorand testnet
transaction. Once a head is anchored, rewriting local history no longer
helps -- the recomputed chain won't match the hash sitting in a block
nobody involved here can edit. That's the part a plain audit log can't do,
and it's why this system is on a chain rather than in a Postgres table.

Daily-cap enforcement is the one thing here that must be race-free: two
concurrent /spend calls for the same agent must not both read "spent so far"
before either has recorded its own spend and both slip under the cap. See
try_reserve() -- it uses SQLite's BEGIN IMMEDIATE to take the write lock
before reading, so a second concurrent reservation blocks until the first
commits or rolls back, closing that check-then-act window.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from common import config

DB_PATH = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    tx_id TEXT,
    explorer_url TEXT,
    -- The resolved call arguments, as canonical JSON ({} when the action
    -- takes none). Stored rather than derived for two reasons: a spend
    -- parked for human approval has to still know which company it was
    -- going to enrich when someone releases it minutes later, and "$0.05
    -- on enrich" is a much weaker audit record than "$0.05 to enrich
    -- Apple Inc." for the operator reading it back.
    params TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    seq INTEGER NOT NULL,
    head_hash TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    explorer_url TEXT,
    note TEXT NOT NULL
);

-- Undo log for the demo tamper button ONLY (see tamper_with_event). Kept
-- in the database rather than in memory so a demo survives a process
-- restart between "show the breakage" and "put it back"; kept in its own
-- table rather than as audit events because it is scaffolding, not
-- history, and must never be mistaken for part of the ledger.
CREATE TABLE IF NOT EXISTS tamper_backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_agent_day
    ON requests (agent_id, decision, timestamp);
"""

# Rows in these states count toward the daily cap. 'pending' is a
# reservation whose payment is still settling; 'awaiting_approval' is one
# parked for a human decision (see policy_engine/app.py). Both hold budget
# for exactly the same reason: a second concurrent request must not be able
# to slip past the cap using budget the first one has already claimed.
CAP_COUNTING_DECISIONS = ("approved", "pending", "awaiting_approval")

# prev_hash of the very first event. Any 32-byte constant works; zeros make
# "this is the genesis entry" obvious when reading the raw table.
GENESIS_HASH = "0" * 64


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # commits on clean exit, rolls back on exception
            yield conn
    finally:
        conn.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does
# nothing to a table that already exists, so a database created before a
# column was introduced needs it added explicitly -- otherwise the schema
# only looks right on a machine that started from an empty data/ directory.
MIGRATIONS = [
    ("requests", "params", "ALTER TABLE requests ADD COLUMN params TEXT NOT NULL DEFAULT '{}'"),
]


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Append-only hash-chained audit ledger
# ---------------------------------------------------------------------------


def canonical_payload(payload: dict) -> str:
    """The exact string that gets hashed and stored.

    Sorted keys and no whitespace, so the same logical payload always
    serializes to identical bytes -- an independent verifier (see
    scripts/verify_audit.py) has to be able to reproduce this byte-for-byte
    from the stored row or every hash check fails for the wrong reason.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_entry_hash(seq: int, timestamp: str, event_type: str, payload_json: str, prev_hash: str) -> str:
    """The chain link. Commits to the event's position (seq), its content,
    and the entire history before it (prev_hash) in one hash.

    "|" is a safe separator here rather than a real length-prefixed encoding
    because every field before payload_json is machine-generated and
    "|"-free (an int, an ISO timestamp, an internal event-type constant),
    and payload_json is last -- so there's no pair of distinct events that
    can serialize to the same string by shifting a delimiter.
    """
    material = f"{seq}|{timestamp}|{event_type}|{payload_json}|{prev_hash}"
    return hashlib.sha256(material.encode()).hexdigest()


def _append_event_locked(conn: sqlite3.Connection, event_type: str, payload: dict) -> dict:
    """Appends one event using an ALREADY-OPEN write transaction.

    Every caller here is a function that has itself already taken the write
    lock (log_request, try_reserve, finalize...). Opening a second
    connection to append would deadlock against that first one -- same
    process, same file, and SQLite only allows one writer. So the
    transaction is the caller's; this just adds a row to it, which also
    makes the audit event and the operational row commit or roll back
    together rather than being able to disagree.
    """
    row = conn.execute("SELECT seq, entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    seq = (row["seq"] + 1) if row else 1
    prev_hash = row["entry_hash"] if row else GENESIS_HASH

    timestamp = _now()
    payload_json = canonical_payload(payload)
    entry_hash = compute_entry_hash(seq, timestamp, event_type, payload_json, prev_hash)

    conn.execute(
        "INSERT INTO audit_events (seq, timestamp, event_type, payload, prev_hash, entry_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (seq, timestamp, event_type, payload_json, prev_hash, entry_hash),
    )
    return {
        "seq": seq,
        "timestamp": timestamp,
        "event_type": event_type,
        "payload": payload_json,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
    }


def append_event(event_type: str, payload: dict) -> dict:
    """Appends one event in its own transaction, for callers that aren't
    already inside one (policy edits, freezes, anchor records)."""
    with _connect() as conn:
        return _append_event_locked(conn, event_type, payload)


def get_audit_events(limit: int = 200, after_seq: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE seq > ? ORDER BY seq DESC LIMIT ?",
            (after_seq, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_chain_head() -> dict | None:
    """The (seq, hash) pair an anchor commits to. None on an empty ledger."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT seq, entry_hash, timestamp FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_event_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]


def verify_chain() -> dict:
    """Recomputes every hash in the ledger and reports the first break.

    Two independent things can be wrong, and they're worth distinguishing
    because they mean different things about the attacker:

      - `entry_hash` doesn't match the event's own recomputed content ->
        someone edited a row's payload/timestamp/type in place and didn't
        (or couldn't) recompute its hash.
      - `prev_hash` doesn't match the actual previous event's hash ->
        someone edited an earlier event and recomputed its hash, but didn't
        cascade the change forward, OR deleted an event outright.

    Returns {"ok", "event_count", "head_seq", "head_hash", "broken_at",
    "detail"}. broken_at is the seq of the first bad event, or None.

    KNOWN LIMIT, stated plainly because it's inherent rather than a bug: a
    hash chain cannot detect an in-place edit of its own LAST event. There
    is no following event whose prev_hash would disagree, and a tamperer who
    recomputes that event's hash leaves something wholly self-consistent.
    Only an anchor closes this, and only once one exists -- which is
    precisely why anchoring runs automatically rather than on request. The
    exposure window is "events appended since the last anchor", and
    common/anchor.py's AUTO_ANCHOR_THRESHOLD is what bounds it.
    """
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY seq ASC").fetchall()

    prev_hash = GENESIS_HASH
    expected_seq = 1
    for row in rows:
        if row["seq"] != expected_seq:
            return {
                "ok": False,
                "event_count": len(rows),
                "head_seq": rows[-1]["seq"] if rows else 0,
                "head_hash": rows[-1]["entry_hash"] if rows else None,
                "broken_at": row["seq"],
                "detail": (
                    f"sequence gap: expected seq {expected_seq}, found {row['seq']} "
                    f"-- {row['seq'] - expected_seq} event(s) deleted from the ledger"
                ),
            }
        if row["prev_hash"] != prev_hash:
            return {
                "ok": False,
                "event_count": len(rows),
                "head_seq": rows[-1]["seq"],
                "head_hash": rows[-1]["entry_hash"],
                "broken_at": row["seq"],
                "detail": (
                    f"event {row['seq']} claims to follow {row['prev_hash'][:16]}… "
                    f"but the actual previous event hashes to {prev_hash[:16]}… "
                    "-- history was rewritten before this point"
                ),
            }
        recomputed = compute_entry_hash(
            row["seq"], row["timestamp"], row["event_type"], row["payload"], row["prev_hash"]
        )
        if recomputed != row["entry_hash"]:
            return {
                "ok": False,
                "event_count": len(rows),
                "head_seq": rows[-1]["seq"],
                "head_hash": rows[-1]["entry_hash"],
                "broken_at": row["seq"],
                "detail": (
                    f"event {row['seq']} ({row['event_type']}) was modified in place: "
                    f"its contents hash to {recomputed[:16]}… but it stores {row['entry_hash'][:16]}…"
                ),
            }
        prev_hash = row["entry_hash"]
        expected_seq += 1

    return {
        "ok": True,
        "event_count": len(rows),
        "head_seq": rows[-1]["seq"] if rows else 0,
        "head_hash": rows[-1]["entry_hash"] if rows else None,
        "broken_at": None,
        "detail": "every event hashes correctly and links to its predecessor",
    }


def hash_at_seq(seq: int) -> str | None:
    """Recomputes the chain head as it stood at `seq`, from current data.

    This is what an anchor gets compared against: the anchor holds the hash
    the ledger reported at the moment it was written, and this recomputes
    what the ledger would report for that same point *now*. They differ
    exactly when history has been rewritten since.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE seq <= ? ORDER BY seq ASC", (seq,)
        ).fetchall()
    if not rows or rows[-1]["seq"] != seq:
        return None
    prev_hash = GENESIS_HASH
    for row in rows:
        prev_hash = compute_entry_hash(
            row["seq"], row["timestamp"], row["event_type"], row["payload"], prev_hash
        )
    return prev_hash


# ---------------------------------------------------------------------------
# Anchors (chain head -> Algorand transaction)
# ---------------------------------------------------------------------------


def record_anchor(seq: int, head_hash: str, tx_id: str, explorer_url: str | None, note: str) -> dict:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO anchors (timestamp, seq, head_hash, tx_id, explorer_url, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), seq, head_hash, tx_id, explorer_url, note),
        )
        _append_event_locked(conn, "audit.anchored", {"seq": seq, "head_hash": head_hash, "tx_id": tx_id})
        return dict(conn.execute("SELECT * FROM anchors WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_anchors(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM anchors ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_latest_anchor() -> dict | None:
    anchors = get_anchors(limit=1)
    return anchors[0] if anchors else None


def unanchored_event_count() -> int:
    """How many events have been appended since the last anchor. Drives the
    auto-anchor threshold in common/anchor.py."""
    with _connect() as conn:
        head = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM audit_events").fetchone()["s"]
        anchored = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM anchors").fetchone()["s"]
        return max(0, head - anchored)


# ---------------------------------------------------------------------------
# Operational request log
# ---------------------------------------------------------------------------


def log_request(
    agent_id: str,
    action: str,
    amount_usd: float,
    decision: str,
    reason: str,
    tx_id: str | None = None,
    explorer_url: str | None = None,
    params: dict | None = None,
) -> dict:
    """Logs a decision that never touched the daily-cap budget (a straight
    deny -- unknown agent/action, per-request limit, frozen agent, etc).
    Approved spends that count toward the cap go through try_reserve()/
    finalize() instead.
    """
    ts = _now()
    params_json = canonical_payload(params or {})
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO requests "
            "(timestamp, agent_id, action, amount_usd, decision, reason, tx_id, explorer_url, params) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, agent_id, action, amount_usd, decision, reason, tx_id, explorer_url, params_json),
        )
        row = dict(conn.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,)).fetchone())
        _append_event_locked(
            conn,
            "request.decided",
            {
                "request_id": row["id"],
                "agent_id": agent_id,
                "action": action,
                "amount_usd": amount_usd,
                "params": params or {},
                "decision": decision,
                "reason": reason,
                "tx_id": tx_id,
            },
        )
        return row


def try_reserve(
    agent_id: str,
    action: str,
    amount_usd: float,
    daily_cap_usd: float,
    decision: str = "pending",
    params: dict | None = None,
) -> dict | None:
    """Atomically checks the daily cap and reserves amount_usd against it.

    Runs the read (sum of today's cap-counting spend) and the insert of the
    reservation row in a single BEGIN IMMEDIATE transaction, so a second
    concurrent call for the same agent can't read the same "spent so far"
    before this one commits -- it blocks (up to the connection timeout) and
    then sees this reservation's amount already counted.

    The UTC day is derived from the timestamp this function is about to
    write, NOT passed in by the caller. Those have to be the same value or
    the check is meaningless: a caller computing "today" a moment before
    calling would, across a UTC midnight boundary, check yesterday's budget
    and then write the row into today. (An earlier version took `day` as a
    parameter and had exactly that gap -- and its tests hardcoded a date,
    so they silently stopped testing anything the next day.)

    `decision` is the state the reservation starts in: 'pending' for a spend
    about to be paid, 'awaiting_approval' for one parked for a human. Both
    hold budget; see CAP_COUNTING_DECISIONS.

    Returns the inserted row, or None if reserving would exceed the cap
    (transaction rolled back, nothing written -- caller should log the
    denial itself via log_request).
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # manual transaction control (BEGIN/COMMIT/ROLLBACK below)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = _now()
        day = ts[:10]
        placeholders = ",".join("?" * len(CAP_COUNTING_DECISIONS))
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS total FROM requests "
            f"WHERE agent_id = ? AND decision IN ({placeholders}) AND substr(timestamp, 1, 10) = ?",
            (agent_id, *CAP_COUNTING_DECISIONS, day),
        ).fetchone()
        spent = float(row["total"])
        # Tolerance: these are floats holding cents, and e.g. 0.01+0.01 is
        # 0.020000000000000004 in IEEE 754 -- without it, spending exactly
        # up to a cap is rejected roughly whenever the rounding lands high.
        if spent + amount_usd > daily_cap_usd + 1e-9:
            conn.execute("ROLLBACK")
            return None

        reason = (
            "reserved, awaiting payment settlement"
            if decision == "pending"
            else "reserved, awaiting human approval"
        )
        cur = conn.execute(
            "INSERT INTO requests (timestamp, agent_id, action, amount_usd, decision, reason, params) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, agent_id, action, amount_usd, decision, reason, canonical_payload(params or {})),
        )
        row_id = cur.lastrowid
        _append_event_locked(
            conn,
            "request.reserved",
            {
                "request_id": row_id,
                "agent_id": agent_id,
                "action": action,
                "amount_usd": amount_usd,
                "params": params or {},
                "state": decision,
            },
        )
        result = dict(conn.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone())
        conn.execute("COMMIT")
        return result
    finally:
        conn.close()


def finalize(
    row_id: int,
    decision: str,
    reason: str,
    tx_id: str | None = None,
    explorer_url: str | None = None,
) -> dict:
    """Resolves a reservation to its real outcome. A 'denied' outcome here
    (payment failed after policy approved it, or a human rejected it)
    naturally stops counting toward the daily cap, since only the states in
    CAP_COUNTING_DECISIONS count.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE requests SET decision = ?, reason = ?, tx_id = ?, explorer_url = ? WHERE id = ?",
            (decision, reason, tx_id, explorer_url, row_id),
        )
        row = dict(conn.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone())
        _append_event_locked(
            conn,
            "request.decided",
            {
                "request_id": row_id,
                "agent_id": row["agent_id"],
                "action": row["action"],
                "amount_usd": row["amount_usd"],
                # Read back off the row rather than taken as an argument:
                # finalize is called from the approval-release path minutes
                # after the reservation was written, and the params that
                # matter are the ones the request was actually made with.
                "params": json.loads(row["params"] or "{}"),
                "decision": decision,
                "reason": reason,
                "tx_id": tx_id,
            },
        )
        return row


def get_request(row_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None


def get_requests(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_approvals() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests WHERE decision = 'awaiting_approval' ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_spend(agent_id: str, day: str) -> float:
    """day is an ISO date string, e.g. '2026-08-08' (UTC). Includes
    in-flight reservations so the dashboard meter reflects committed spend,
    not just settled spend.
    """
    placeholders = ",".join("?" * len(CAP_COUNTING_DECISIONS))
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS total FROM requests "
            f"WHERE agent_id = ? AND decision IN ({placeholders}) AND substr(timestamp, 1, 10) = ?",
            (agent_id, *CAP_COUNTING_DECISIONS, day),
        ).fetchone()
        return float(row["total"])


def count_recent_requests(agent_id: str, window_seconds: int) -> int:
    """How many requests this agent has made in the last `window_seconds`,
    counting every decision -- the point of a velocity limit is to catch a
    runaway loop, and a loop that's being denied every time is still a
    runaway loop worth stopping.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE agent_id = ? AND timestamp > ?",
            (agent_id, cutoff_iso),
        ).fetchone()
        return int(row["n"])


def reset() -> None:
    """Wipes the operational request log for demo rehearsals.

    Deliberately does NOT touch audit_events or anchors. An append-only
    ledger you can quietly truncate isn't append-only, so clearing the demo
    log is itself recorded *in* the ledger as an event -- and the on-chain
    anchors covering the wiped period stay valid and still verify.
    """
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
        conn.execute("DELETE FROM requests")
        _append_event_locked(conn, "audit.reset", {"requests_cleared": n})


def get_agent_summary(agent_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN decision = 'approved' THEN amount_usd ELSE 0 END), 0) AS total_spend_usd, "
            "  COUNT(CASE WHEN decision = 'approved' THEN 1 END) AS approved_count, "
            "  COUNT(CASE WHEN decision = 'denied' THEN 1 END) AS denied_count "
            "FROM requests WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Demo-only: deliberate tampering
# ---------------------------------------------------------------------------


def tamper_with_event(seq: int, new_payload_fragment: dict) -> dict:
    """Rewrites a historical audit event IN PLACE, bypassing the append-only
    API -- exactly what a malicious operator with database access would do.

    This exists solely so the tamper-detection story can be demonstrated
    rather than asserted: call it, then call verify_chain() and watch it
    name this seq. It is never reachable from any agent-facing path; the
    only caller is a clearly-labeled demo endpoint (see
    policy_engine/app.py's POST /admin/demo/tamper).

    Deliberately recomputes `entry_hash` to match the doctored payload --
    i.e. it simulates a *competent* tamperer, not a lazy one. That still
    fails verification, because the NEXT event's prev_hash no longer
    matches. And even an attacker who cascades the recomputation all the
    way to the head is caught by the on-chain anchor, which holds the
    original head hash in a block they can't rewrite.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM audit_events WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            raise KeyError(f"no audit event with seq {seq}")
        conn.execute(
            "INSERT INTO tamper_backups (seq, payload, entry_hash) VALUES (?, ?, ?)",
            (seq, row["payload"], row["entry_hash"]),
        )
        payload = json.loads(row["payload"])
        original = {k: payload.get(k) for k in new_payload_fragment}
        payload.update(new_payload_fragment)
        payload_json = canonical_payload(payload)
        new_hash = compute_entry_hash(
            row["seq"], row["timestamp"], row["event_type"], payload_json, row["prev_hash"]
        )
        conn.execute(
            "UPDATE audit_events SET payload = ?, entry_hash = ? WHERE seq = ?",
            (payload_json, new_hash, seq),
        )
        return {"seq": seq, "original": original, "replaced_with": new_payload_fragment}


def restore_tampered() -> dict:
    """Undoes every demo tamper, newest first, restoring the exact original
    bytes and hash so the chain verifies again.

    Purely demo scaffolding: it exists so "break it, prove we caught it, put
    it back" is repeatable in front of an audience instead of a one-shot
    that leaves the dashboard permanently red. Restoring is deliberately
    byte-exact rather than a recompute -- a "repair" that recomputed hashes
    would be indistinguishable from the attack it's undoing.
    """
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tamper_backups ORDER BY id DESC").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE audit_events SET payload = ?, entry_hash = ? WHERE seq = ?",
                (row["payload"], row["entry_hash"], row["seq"]),
            )
        conn.execute("DELETE FROM tamper_backups")
        return {"restored": [r["seq"] for r in rows]}
