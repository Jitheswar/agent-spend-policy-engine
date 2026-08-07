"""SQLite-backed audit trail for the policy engine.

One table, one job: every spend request an agent makes gets a row, approved
or denied, before or after the payment attempt. This is the audit trail the
dashboard reads from.

Daily-cap enforcement is the one thing here that must be race-free: two
concurrent /spend calls for the same agent must not both read "spent so far"
before either has recorded its own spend and both slip under the cap. See
try_reserve() -- it uses SQLite's BEGIN IMMEDIATE to take the write lock
before reading, so a second concurrent reservation blocks until the first
commits or rolls back, closing that check-then-act window.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "policy_engine.db")

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
    explorer_url TEXT
);
"""

# Rows in this state count toward the daily cap (a reservation in flight)
# exactly like 'approved' does, so a second concurrent request can't slip
# past the cap while the first one's payment is still settling.
CAP_COUNTING_DECISIONS = ("approved", "pending")


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


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)


def log_request(
    agent_id: str,
    action: str,
    amount_usd: float,
    decision: str,
    reason: str,
    tx_id: str | None = None,
    explorer_url: str | None = None,
) -> dict:
    """Logs a decision that never touched the daily-cap budget (a straight
    deny -- unknown agent/action, per-request limit, etc). Approved spends
    that count toward the cap go through try_reserve()/finalize() instead.
    """
    ts = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO requests (timestamp, agent_id, action, amount_usd, decision, reason, tx_id, explorer_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, agent_id, action, amount_usd, decision, reason, tx_id, explorer_url),
        )
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def try_reserve(agent_id: str, action: str, amount_usd: float, daily_cap_usd: float, day: str) -> dict | None:
    """Atomically checks the daily cap and reserves amount_usd against it.

    Runs the read (sum of today's approved+pending spend) and the insert of
    a 'pending' row in a single BEGIN IMMEDIATE transaction, so a second
    concurrent call for the same agent can't read the same "spent so far"
    before this one commits -- it blocks (up to the connection timeout) and
    then sees this reservation's amount already counted.

    Returns the inserted 'pending' row, or None if reserving would exceed
    the cap (transaction rolled back, nothing written -- caller should log
    the denial itself via log_request).
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # manual transaction control (BEGIN/COMMIT/ROLLBACK below)
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" * len(CAP_COUNTING_DECISIONS))
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS total FROM requests "
            f"WHERE agent_id = ? AND decision IN ({placeholders}) AND substr(timestamp, 1, 10) = ?",
            (agent_id, *CAP_COUNTING_DECISIONS, day),
        ).fetchone()
        spent = float(row["total"])
        if spent + amount_usd > daily_cap_usd:
            conn.execute("ROLLBACK")
            return None

        ts = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO requests (timestamp, agent_id, action, amount_usd, decision, reason) "
            "VALUES (?, ?, ?, ?, 'pending', 'reserved, awaiting payment settlement')",
            (ts, agent_id, action, amount_usd),
        )
        row_id = cur.lastrowid
        conn.execute("COMMIT")
        result = conn.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone()
        return dict(result)
    finally:
        conn.close()


def finalize(
    row_id: int,
    decision: str,
    reason: str,
    tx_id: str | None = None,
    explorer_url: str | None = None,
) -> dict:
    """Resolves a 'pending' reservation to its real outcome. A 'denied'
    outcome here (payment failed after policy approved it) naturally stops
    counting toward the daily cap, since only approved/pending rows count.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE requests SET decision = ?, reason = ?, tx_id = ?, explorer_url = ? WHERE id = ?",
            (decision, reason, tx_id, explorer_url, row_id),
        )
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone()
        return dict(row)


def get_requests(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_spend(agent_id: str, day: str) -> float:
    """day is an ISO date string, e.g. '2026-08-07' (UTC). Includes
    'pending' reservations so the dashboard meter reflects in-flight spend,
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


def reset() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM requests")


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
